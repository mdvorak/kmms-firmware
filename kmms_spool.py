# KMMS Spool config
#
# Based on
# https://github.com/Klipper3d/klipper/blob/3417940fd82adf621f429f42289d3693ee832582/klippy/extras/output_pin.py
#
# Copyright (C) 2023-2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
from extras.filament_switch_sensor import SwitchSensor

PIN_MIN_TIME = 0.100
RESEND_HOST_TIME = 0.300 + PIN_MIN_TIME
MAX_SCHEDULE_TIME = 5.0
EVENT_DELAY = 3.0


class KmmsSpool(SwitchSensor, object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        pins = self.printer.lookup_object('pins')
        buttons = self.printer.load_object(config, 'buttons')

        # Filament Sensor
        buttons.register_buttons([config.get('filament_sensor_pin')], self._button_handler)

        self.min_event_systime = self.reactor.NEVER
        self.filament_present = False
        self.sensor_enabled = True

        # Motor PWM
        self.load_pin = pins.setup_pin('pwm', config.get('load_pin'))
        self.unload_pin = pins.setup_pin('pwm', config.get('unload_pin'))

        self.load_power = config.getfloat('load_power', 1., minval=0.01, maxval=1.)
        self.unload_power = config.getfloat('unload_power', 1., minval=0.01, maxval=1.)

        cycle_time = config.getfloat('cycle_time', 0.01, above=0.,
                                     maxval=MAX_SCHEDULE_TIME)
        hardware_pwm = config.getboolean('hardware_pwm', False)

        self.load_pin.setup_cycle_time(cycle_time, hardware_pwm)
        self.unload_pin.setup_cycle_time(cycle_time, hardware_pwm)

        self.last_cycle_time = self.default_cycle_time = cycle_time
        self.last_print_time = 0.

        self.reactor = self.printer.get_reactor()
        self.resend_timer = None
        self.resend_interval = 0.

        max_mcu_duration = config.getfloat('maximum_mcu_duration', MAX_SCHEDULE_TIME,
                                           minval=0.500,
                                           maxval=MAX_SCHEDULE_TIME)
        self.load_pin.setup_max_duration(max_mcu_duration)
        self.unload_pin.setup_max_duration(max_mcu_duration)
        if max_mcu_duration:
            self.resend_interval = max_mcu_duration - RESEND_HOST_TIME

        self.last_value = 0.
        self.load_pin.setup_start_value(self.last_value, 0.)

        # Register events and commands
        self.full_name = config.get_name()
        self.name = self.full_name.split()[-1]

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        self.gcode.register_mux_command("SET_PIN", "PIN", self.name,
                                        self.cmd_SET_PIN,
                                        desc=self.cmd_SET_PIN_help)

        self.gcode.register_mux_command("QUERY_FILAMENT_SENSOR", "SENSOR", self.name,
                                        self.cmd_QUERY_FILAMENT_SENSOR,
                                        desc=self.cmd_QUERY_FILAMENT_SENSOR_help)

    def _exec_gcode(self, prefix, template):
        try:
            self.gcode.run_script(prefix + template.render() + "\nM400")
        except Exception:
            logging.exception("Script running error")
        self.min_event_systime = self.reactor.monotonic() + EVENT_DELAY

    def _is_printing(self, eventtime):
        idle_timeout = self.printer.lookup_object("idle_timeout")
        return idle_timeout.get_status(None)["state"] == "Printing"

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.

    def _button_handler(self, eventtime, state):
        if state == self.filament_present:
            return

        self.filament_present = state
        if self.last_value == 0.:
            # Not active
            if self.filament_present:
                self._insert_event_handler(eventtime)
            else:
                self._runout_event_handler(eventtime)
        else:
            # Currently active, handle event
            self._stop_event_handler(eventtime)

    def _insert_event_handler(self, eventtime):
        if self._is_printing(eventtime):
            logging.info("[%s] Filament inserted while printing, skipping event" % self.full_name)
            return

        # self._exec_gcode("", self.insert_gcode)

    def _runout_event_handler(self, eventtime):
        pass

    def _stop_event_handler(self, eventtime):
        pass

    def get_status(self, eventtime):
        return {
            'filament_detected': bool(self.filament_present),
            'value': self.last_value
        }

    def set_pin(self, print_time, value, cycle_time=None, is_resend=False):
        if cycle_time is None:
            cycle_time = self.default_cycle_time

        if value == self.last_value and cycle_time == self.last_cycle_time:
            if not is_resend:
                return
        print_time = max(print_time, self.last_print_time + PIN_MIN_TIME)

        if value >= 0.:
            self.unload_pin.set_pwm(print_time, 0., cycle_time)
            self.load_pin.set_pwm(print_time, value, cycle_time)
        else:
            self.load_pin.set_pwm(print_time, 0., cycle_time)
            self.unload_pin.set_pwm(print_time, abs(value), cycle_time)

        self.last_value = value
        self.last_cycle_time = cycle_time
        self.last_print_time = print_time
        if self.resend_interval and self.resend_timer is None:
            self.resend_timer = self.reactor.register_timer(
                self._resend_current_val, self.reactor.NOW)

    cmd_SET_PIN_help = "Set the value of an output pin"

    def cmd_SET_PIN(self, gcmd):
        value = gcmd.get_float('VALUE', minval=-1., maxval=1.)
        cycle_time = gcmd.get_float('CYCLE_TIME', self.default_cycle_time,
                                    above=0., maxval=MAX_SCHEDULE_TIME)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(
            lambda print_time: self.set_pin(print_time, value, cycle_time))

    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"

    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "Filament Sensor %s: filament detected" % self.name
        else:
            msg = "Filament Sensor %s: filament not detected" % self.name
        gcmd.respond_info(msg)

    def _resend_current_val(self):
        if self.last_value == 0.:
            self.reactor.unregister_timer(self.resend_timer)
            self.resend_timer = None
            return self.reactor.NEVER

        systime = self.reactor.monotonic()
        print_time = self.load_pin.get_mcu().estimated_print_time(systime)
        time_diff = (self.last_print_time + self.resend_interval) - print_time
        if time_diff > 0.:
            # Reschedule for resend time
            return systime + time_diff
        self.set_pin(print_time + PIN_MIN_TIME,
                     self.last_value, self.last_cycle_time, True)
        return systime + self.resend_interval


def load_config_prefix(config):
    return KmmsSpool(config)
