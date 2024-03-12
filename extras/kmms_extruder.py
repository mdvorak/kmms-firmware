import logging

import chelper
from configfile import ConfigWrapper
from kinematics.extruder import ExtruderStepper, PrinterExtruder, DummyExtruder
from klippy import Printer
from toolhead import ToolHead


# This class mimics PrinterExtruder
class KmmsExtruder:
    printer: Printer
    toolhead: ToolHead

    def __init__(self, config: ConfigWrapper):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.full_name = config.get_name()
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()

        self.extruder_stepper = ExtruderStepper(config)
        self.generate_steps = self.extruder_stepper.stepper.generate_steps

        self.max_velocity = config.getfloat('max_velocity', above=0.)
        self.max_accel = config.getfloat('max_accel', above=0.)
        self.last_position = 0.
        self.prev_extruder = None

        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves

        # Event handlers
        self.printer.register_event_handler("klippy:connect", self._handle_connect)

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')

    def _configure_extruder_stepper(self, trapq, pos, motion_queue=None):
        self.extruder_stepper.stepper.set_position([pos, 0., 0.])
        self.extruder_stepper.stepper.set_trapq(trapq)
        self.extruder_stepper.motion_queue = motion_queue

        if trapq:
            if self.generate_steps not in self.toolhead.step_generators:
                self.toolhead.register_step_generator(self.generate_steps)
        else:
            self.toolhead.step_generators.remove(self.generate_steps)

    def sync_to_extruder(self, extruder_name):
        # NOTE we intentionally don't use ExtruderStepper.sync_to_extruder, since that method has limitations
        self.toolhead.flush_step_generation()

        if self.toolhead.get_extruder() is self:
            raise self.printer.command_error("Cannot set sync while this extruder is active")

        if not extruder_name:
            self._configure_extruder_stepper(None, 0.)
            return

        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None or not isinstance(extruder, (PrinterExtruder, KmmsExtruder,)):
            raise self.printer.command_error("'%s' is not a valid extruder." % extruder_name)

        self.logger.info("Syncing to extruder %s", extruder_name)
        self._configure_extruder_stepper(extruder.get_trapq(), extruder.last_position, motion_queue=extruder_name)

    def is_active(self):
        return self.toolhead.get_extruder() is self

    def activate(self):
        if self.toolhead.get_extruder() is self:
            self.logger.info("Extruder already active")
            return

        self.logger.info("Activating extruder")

        self.toolhead.flush_step_generation()
        self._configure_extruder_stepper(self.trapq, self.last_position)
        self.prev_extruder = self.toolhead.get_extruder()
        self.toolhead.set_extruder(self, self.last_position)
        self.printer.send_event("extruder:activate_extruder")

    def deactivate(self):
        if self.toolhead.get_extruder() is self:
            self.logger.debug("Extruder is not active")
            return

        self.logger.info("Deactivating extruder")

        self.toolhead.flush_step_generation()
        self.toolhead.set_extruder(self.prev_extruder, self.prev_extruder.last_position)
        self.prev_extruder = None
        self._configure_extruder_stepper(None, 0.)
        self.printer.send_event("extruder:activate_extruder")

    def set_last_position(self, pos):
        self.last_position = pos

    def update_move_time(self, flush_time, clear_history_time):
        self.trapq_finalize_moves(self.trapq, flush_time, clear_history_time)

    def check_move(self, move):
        if move.axes_d[0] or move.axes_d[1] or move.axes_d[2]:
            raise self.printer.command_error("extruder_stepper %s cannot be used in conjunction with other movements")

        move.limit_speed(self.max_velocity, self.max_accel)

    def move(self, print_time, move):
        axis_r = move.axes_r[3]
        accel = move.accel * axis_r
        start_v = move.start_v * axis_r
        cruise_v = move.cruise_v * axis_r
        can_pressure_advance = False # PA is not supported, since we do only standalone movements
        # Queue movement (x is extruder movement, y is pressure advance flag)
        self.trapq_append(self.trapq, print_time,
                          move.accel_t, move.cruise_t, move.decel_t,
                          move.start_pos[3], 0., 0.,
                          1., can_pressure_advance, 0.,
                          start_v, cruise_v, accel)
        self.last_position = move.end_pos[3]

    def find_past_position(self, print_time):
        return self.extruder_stepper.find_past_position(print_time)

    def calc_junction(self, prev_move, move):
        return move.max_cruise_v2

    def get_name(self):
        return self.name

    def get_heater(self):
        raise self.printer.command_error("KMMS Extruder does not have a heater")

    def get_trapq(self):
        return self.trapq

    def get_status(self, eventtime):
        return self.extruder_stepper.get_status(eventtime) | {
            'can_extrude': True
        }
