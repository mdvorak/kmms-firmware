# KMMS
#
# Copyright (C) 2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

import chelper
from extras.kmms_path import KmmsPath
from gcode import GCodeDispatch
from klippy import Printer
from mcu import MCU_trsync, TRSYNC_TIMEOUT
from reactor import Reactor, ReactorCompletion
from toolhead import ToolHead


class KmmsError(Exception):
    pass


# noinspection SpellCheckingInspection
class KmmsVirtualEndstop:
    reactor: Reactor

    def __init__(self, printer: Printer):
        self.reactor = printer.get_reactor()
        self.waiting = (self.reactor.completion(), [])

        ffi_main, ffi_lib = chelper.get_ffi()
        self._trdispatch = ffi_main.gc(ffi_lib.trdispatch_alloc(), ffi_lib.free)
        self._trsyncs = [MCU_trsync(mcu, self._trdispatch) for _, mcu in printer.lookup_objects(module='mcu')]
        self._trsyncs_map = {trsync.get_mcu(): trsync for trsync in self._trsyncs}
        self._main_trsync = self._trsyncs[0]

        printer.register_event_handler("kmms:filament_insert", self._handle_filament_insert)

    def _handle_filament_insert(self, eventtime, full_name):
        completion, names = self.waiting
        if full_name in names and not completion.test():
            completion.complete(full_name)

    def add_stepper(self, stepper):
        stepper_mcu = stepper.get_mcu()
        self._trsyncs_map[stepper_mcu].add_stepper(stepper)

    def start(self, print_time, names) -> ReactorCompletion:
        completion = self.reactor.completion()

        prev, _ = self.waiting
        self.waiting = (completion, list(names))

        # Make sure we don't leave anyone hanging
        if not prev.test():
            prev.complete(None)

        for i, trsync in enumerate(self._trsyncs):
            report_offset = float(i) / len(self._trsyncs)
            trsync.start(print_time, report_offset,
                         completion, TRSYNC_TIMEOUT)

        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_start(self._trdispatch, self._main_trsync.REASON_HOST_REQUEST)

        return completion

    def stop(self):
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_stop(self._trdispatch)
        return [trsync.stop() for trsync in self._trsyncs]

    def wait(self, print_time):
        eventtime = self._main_trsync.get_mcu().print_time_to_clock(print_time)
        return self.waiting[0].wait(eventtime)


class Kmms:
    printer: Printer
    reactor: Reactor
    gcode: GCodeDispatch
    toolhead: ToolHead
    paths: dict[str, KmmsPath]
    active_path: KmmsPath

    def __init__(self, config):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.path = self.printer.load_object(config, 'kmms_path')
        self.endstop = KmmsVirtualEndstop(self.printer)

        # Read configuration
        self.max_velocity = config.getfloat('max_velocity', above=0.)
        self.max_accel = config.getfloat('max_accel', above=0.)

        # Register event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("kmms:filament_insert", self._handle_filament_runout)
        self.printer.register_event_handler("kmms:filament_runout", self._handle_filament_insert)

        self.gcode.register_command("KMMS_ACTIVATE_EXTRUDERS", self.cmd_KMMS_ACTIVATE_EXTRUDERS)
        self.gcode.register_command("KMMS_PRELOAD", self.cmd_KMMS_PRELOAD)
        self.gcode.register_command("KMMS_STATUS", self.cmd_KMMS_STATUS)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        # Load paths
        self.paths = dict((n.removeprefix('kmms_path '), m) for n, m in self.printer.lookup_objects('kmms_path')
                          if n != 'kmms_path')
        self.active_path = self.paths['spool_0']

        # Init - this will populate path objects
        self.printer.send_event('kmms:init')

        # Register steppers to endstop
        for path in self.paths.values():
            for extruder in path.get_objects(self.path.EXTRUDER):
                self.endstop.add_stepper(extruder.extruder_stepper.stepper)

    def _handle_filament_insert(self, eventtime, full_name):
        pass

    def _handle_filament_runout(self, eventtime, full_name):
        pass

    def move_to_toolhead(self):
        path = self.active_path
        eventtime = self.reactor.monotonic()

        if len(path) < 1:
            raise self.printer.command_error("No filament is selected")

        # Find all extruders
        extruders = path.find_path_items(path.EXTRUDER)
        if len(extruders) < 1:
            raise self.printer.config_error(
                "Path '%s' does not have toolhead extruder configured" % self.active_path.name)

        # Get toolhead extruder
        toolhead_pos, toolhead_extruder = extruders.pop()

        # Find current position
        pos, _ = path.find_path_position(eventtime)
        if pos < 0:
            raise KmmsError("It seems to be empty")
        if pos >= toolhead_pos:
            self.gcode.respond_info("%s is already at toolhead" % path.name)
            return False

        # Desync all known extruders
        self.logger.info('Desync extruders')

        # Find last extruder before toolhead
        if len(extruders) < 1:
            raise self.printer.config_error("Path '%s' does not have any extruders configured" % self.active_path.name)
        drive_extruder_pos, drive_extruder = extruders.pop()

        # Find last sensor before toolhead
        toolhead_sensor_pos, toolhead_sensor = path.find_path_last(path.SENSOR, toolhead_pos)

        # Find backpressure sensors between last toolhead and drive extruders
        backpressure_sensors = [bp for _, bp in
                                path.find_path_items(path.BACKPRESSURE, drive_extruder_pos, toolhead_pos)]

        # Move to toolhead
        # TODO this can be handled with static distances later
        if toolhead_sensor is None and len(backpressure_sensors) < 1:
            raise KmmsError("KMMS: %s does not have any sensors before toolhead configured" % path.name)

        lines = [f'{obj.name}=>{obj.filament_detected(0)}' for _, obj in
                 path.find_path_items(path.BACKPRESSURE, drive_extruder_pos, toolhead_pos)]
        self.gcode.respond_info("KMMS:\n    %s" % '\n    '.join(lines))

        try:
            # Handle case, where filament is already pressed against extruder
            if (toolhead_sensor.filament_detected(eventtime) and
                    all(bp.filament_detected(eventtime) for bp in backpressure_sensors)):
                self.gcode.respond_info("%s seems to be at toolhead" % path.name)
                return True

            # Activate extruders
            self.printer.send_event('kmms:desync')
            # drive_extruder.obj.set_last_position(0)
            self._activate_extruder(drive_extruder.name)
            active_extruders = [self.toolhead.get_extruder()]

            for _, extruder in extruders:
                self._sync_to_extruder(extruder.name, drive_extruder.name)
                active_extruders.append(extruder.get_object())

            self.gcode.respond_info("KMMS: Moving to '%s'" % toolhead_sensor.name)

            endstop_names = [toolhead_sensor.name] + [bp.name for bp in backpressure_sensors]
            print_time = self.toolhead.get_last_move_time()
            move_completion = self.endstop.start(print_time, endstop_names)

            self.toolhead.flush_step_generation()
            self.toolhead.dwell(0.001)

            # TODO create some sort of coords system
            # self.set_position(0)
            initial_pos = self.get_position()
            self.toolhead.drip_move(self.relative_pos(100), self.max_velocity, move_completion)  # TODO pos

            # Wait for move to finish
            endstop_hit = self.endstop.wait(self.toolhead.get_last_move_time())
            self.endstop.stop()
            self.toolhead.flush_step_generation()

            self.gcode.respond_info(
                "KMMS: Moved %.1f mm, hit %s endstop" % (self.get_position() - initial_pos, endstop_hit))
            return True
        finally:
            # Make sure expected extruder is always activated
            self.toolhead.register_lookahead_callback(lambda print_time: self.activate_path_extruders())

    def get_position(self):
        return self.toolhead.get_position()[3]

    def set_position(self, e):
        pos = self.toolhead.get_position()
        pos[3] = e
        self.toolhead.set_position(pos)

    def relative_pos(self, e: float):
        pos = self.toolhead.get_position()
        pos[3] += e
        return pos

    def activate_path_extruders(self):
        extruders = self.active_path.get_objects(self.path.EXTRUDER)
        if len(extruders) < 1:
            raise self.printer.config_error(
                "Path '%s' does not have toolhead extruder configured" % self.active_path.name)
        toolhead_extruder = extruders.pop()

        self.printer.send_event('kmms:desync')
        self._activate_extruder(toolhead_extruder.name)
        for e in extruders:
            self._sync_to_extruder(e.name, toolhead_extruder.name)

    def _activate_extruder(self, extruder_name: str):
        self.logger.info('Activating extruder %s', extruder_name)
        self.gcode.run_script_from_command('ACTIVATE_EXTRUDER EXTRUDER="%s"' % extruder_name)

    def _sync_to_extruder(self, extruder_name: str, motion_queue: str):
        self.logger.info('Syncing extruder %s to %s', extruder_name, motion_queue)
        self.gcode.run_script_from_command(
            'SYNC_EXTRUDER_MOTION EXTRUDER="%s" MOTION_QUEUE="%s"' % (extruder_name, motion_queue))

    def cmd_KMMS_ACTIVATE_EXTRUDERS(self, gcmd):
        self.activate_path_extruders()

    def cmd_KMMS_PRELOAD(self, gcmd):
        try:
            self.move_to_toolhead()
        except KmmsError as e:
            self.gcode.respond_info('KMMS Error: %s' % e)

    def cmd_KMMS_STATUS(self, gcmd):
        eventtime = self.reactor.monotonic()
        lines = ["{}\t/\t{}\t=\t{}".format(i.name, k, v) for i in self.active_path.get_path_items() for k, v in
                 list(i.get_status(eventtime).items()) + [('flags', i.flags)]]
        self.gcode.respond_info("KMMS %s:\n    %s" % (self.active_path.name, '\n    '.join(lines),))


def load_config(config):
    return Kmms(config)
