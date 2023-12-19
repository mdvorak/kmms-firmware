# KMMS Spool config
#
# Based on
# https://github.com/Klipper3d/klipper/blob/3417940fd82adf621f429f42289d3693ee832582/klippy/extras/output_pin.py
#
# Copyright (C) 2023-2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

PIN_MIN_TIME = 0.100
RESEND_HOST_TIME = 0.300 + PIN_MIN_TIME
MAX_SCHEDULE_TIME = 5.0


class KmmsSpool:
    def __init__(self, config):
        self.printer = config.get_printer()
        pins = self.printer.lookup_object('pins')

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

        pin_name = config.get_name().split()[1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("SET_PIN", "PIN", pin_name,
                                   self.cmd_SET_PIN,
                                   desc=self.cmd_SET_PIN_help)

    def get_status(self):
        return {'value': self.last_value}

    def set_pin(self, print_time, value, cycle_time=None, is_resend=False):
        if cycle_time is None:
            cycle_time = cycle_time

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
