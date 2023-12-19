# KMMS Spool config
#
# Based on
# https://github.com/Klipper3d/klipper/blob/3417940fd82adf621f429f42289d3693ee832582/klippy/extras/output_pin.py
#
# Copyright (C) 2023-2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

PIN_MIN_TIME = 0.100
RESEND_HOST_TIME = 0.300 + PIN_MIN_TIME
MAX_SCHEDULE_TIME = 5.0


class KmmsSpool(object):
    def __init__(self, config):
        self.full_name = config.get_name()
        self.name = self.full_name.split()[-1]
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        pins = self.printer.lookup_object('pins')

        # Filament Sensor
        self.filament_switch = self._define_filament_switch(config, self.name, config.get('filament_sensor_pin'))

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
        self.gcode.register_mux_command("SET_PIN", "PIN", self.name,
                                        self.cmd_SET_PIN,
                                        desc=self.cmd_SET_PIN_help)

    def get_status(self, eventtime):
        filament_status = self.filament_switch.get_status(eventtime)

        return {
            'filament_detected': filament_status['filament_detected'],
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

    # Helpers

    def _resend_current_val(self, eventtime):
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

    def _is_printing(self, eventtime):
        idle_timeout = self.printer.lookup_object("idle_timeout")
        return idle_timeout.get_status(None)["state"] == "Printing"

    def _define_filament_switch(self, config, name, switch_pin):
        section = "filament_switch_sensor %s" % name
        insert_gcode = "__KMMS_SPOOL_FILAMENT_INSERT SPOOL=%s" % name
        runout_gcode = "__KMMS_SPOOL_FILAMENT_RUNOUT SPOOL=%s" % name

        config.fileconfig.add_section(section)
        config.fileconfig.set(section, "switch_pin", switch_pin)
        config.fileconfig.set(section, "pause_on_runout", "False")
        config.fileconfig.set(section, "insert_gcode", insert_gcode)
        config.fileconfig.set(section, "runout_gcode", runout_gcode)

        fs = self.printer.load_object(config, section)

        # Replace with custom runout_helper, because original fires runout event only during print
        custom_helper = SpoolRunoutHelper(self.printer, name, insert_gcode, runout_gcode)
        fs.runout_helper = custom_helper
        fs.get_status = custom_helper.get_status

        return fs


class SpoolRunoutHelper:
    # Copy from
    # https://github.com/moggieuk/Happy-Hare/blob/63565368c072bff2673a7a97469fb7663f62ee12/extras/mmu_sensors.py

    def __init__(self, printer, name, insert_gcode, runout_gcode):
        self.printer, self.name = printer, name
        self.insert_gcode, self.runout_gcode = insert_gcode, runout_gcode
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.min_event_systime = self.reactor.NEVER
        self.event_delay = 1.  # Time between generated events
        self.filament_present = False
        self.sensor_enabled = True

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        # We are going to replace previous runout_helper mux commands with ours
        prev = self.gcode.mux_commands.get("QUERY_FILAMENT_SENSOR")
        prev_key, prev_values = prev
        prev_values[self.name] = self.cmd_QUERY_FILAMENT_SENSOR

        prev = self.gcode.mux_commands.get("SET_FILAMENT_SENSOR")
        prev_key, prev_values = prev
        prev_values[self.name] = self.cmd_SET_FILAMENT_SENSOR

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.  # Time to wait until events are processed

    def _insert_event_handler(self, eventtime):
        self._exec_gcode(self.insert_gcode)

    def _runout_event_handler(self, eventtime):
        self._exec_gcode(self.runout_gcode)

    def _exec_gcode(self, command):
        try:
            self.gcode.run_script(command)
        except Exception:
            logging.exception("Error running filament switch handler: `%s`" % command)
        self.min_event_systime = self.reactor.monotonic() + self.event_delay

    def note_filament_present(self, is_filament_present):
        if is_filament_present == self.filament_present:
            return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()

        # Don't handle too early or if disabled
        if eventtime < self.min_event_systime or not self.sensor_enabled:
            return

        # Let handler decide what processing is possible based on current state
        if is_filament_present:  # Insert detected
            self.min_event_systime = self.reactor.NEVER
            logging.info("Spool filament sensor %s: insert event detected, Time %.2f" % (self.name, eventtime))
            self.reactor.register_callback(self._insert_event_handler)
        else:  # Runout detected
            self.min_event_systime = self.reactor.NEVER
            logging.info("Spool filament sensor %s: runout event detected, Time %.2f" % (self.name, eventtime))
            self.reactor.register_callback(self._runout_event_handler)

    def get_status(self, eventtime):
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled),
        }

    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"

    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "Spool filament sensor %s: filament detected" % self.name
        else:
            msg = "Spool filament sensor %s: filament not detected" % self.name
        gcmd.respond_info(msg)

    cmd_SET_FILAMENT_SENSOR_help = "Sets the filament sensor on/off"

    def cmd_SET_FILAMENT_SENSOR(self, gcmd):
        self.sensor_enabled = gcmd.get_int("ENABLE", 1)


def load_config_prefix(config):
    return KmmsSpool(config)
