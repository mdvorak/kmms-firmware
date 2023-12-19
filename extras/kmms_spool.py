# KMMS Spool config
#
# Based on
# https://github.com/Klipper3d/klipper/blob/3417940fd82adf621f429f42289d3693ee832582/klippy/extras/output_pin.py
#
# Copyright (C) 2023-2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

PIN_MIN_TIME = 0.010
RESEND_HOST_TIME = 0.300 + PIN_MIN_TIME
MAX_SCHEDULE_TIME = 5.0


class KmmsSpool(object):
    STATUS_IDLE = "Idle"
    STATUS_LOADING = "Loading"
    STATUS_UNLOADING = "Unloading"

    def __init__(self, config):
        self.full_name = config.get_name()
        self.name = self.full_name.split()[-1]
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        self.toolhead = None
        pins = self.printer.lookup_object('pins')

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        # Filament Sensor
        self.filament_switch = self._define_filament_switch(config, self.name, config.get('filament_sensor_pin'))

        # Motor PWM
        self.load_pin = pins.setup_pin('pwm', config.get('load_pin'))
        self.unload_pin = pins.setup_pin('pwm', config.get('unload_pin'))

        self.load_power = config.getfloat('load_power', 1., minval=0.01, maxval=1.)
        self.unload_power = config.getfloat('unload_power', 1., minval=0.01, maxval=1.)
        self.timeout = config.getfloat('timeout', 15.0, minval=PIN_MIN_TIME, maxval=120)
        self.release_pulse = config.getfloat('release_pulse', 0.020,
                                             minval=PIN_MIN_TIME, maxval=MAX_SCHEDULE_TIME)
        self.release_pulse_power = config.getfloat('release_pulse_power', .5,
                                                   minval=0.01, maxval=1.)

        cycle_time = config.getfloat('cycle_time', 0.01, above=0.,
                                     maxval=MAX_SCHEDULE_TIME)
        hardware_pwm = config.getboolean('hardware_pwm', False)

        self.load_pin.setup_cycle_time(cycle_time, hardware_pwm)
        self.unload_pin.setup_cycle_time(cycle_time, hardware_pwm)

        self.last_cycle_time = self.default_cycle_time = cycle_time
        self.last_print_time = 0.

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

        # State tracking
        self.status = self.STATUS_IDLE
        self.last_duration = self.last_start = None
        self._trigger_completion = None
        self._timeout_timer = None

        self.printer.register_event_handler("kmms_spool:runout", self._handle_runout)

        # Register commands
        self.gcode.register_mux_command("SET_PIN", "PIN", self.name,
                                        self.cmd_SET_PIN,
                                        desc=self.cmd_SET_PIN_help)

        self.gcode.register_mux_command("KMMS_SPOOL_STOP", "SPOOL", self.name,
                                        self.cmd_KMMS_SPOOL_STOP,
                                        desc=self.cmd_KMMS_SPOOL_STOP_help)

        self.gcode.register_mux_command("KMMS_SPOOL_LOAD", "SPOOL", self.name,
                                        self.cmd_KMMS_SPOOL_LOAD,
                                        desc=self.cmd_KMMS_SPOOL_LOAD_help)

        self.gcode.register_mux_command("KMMS_SPOOL_UNLOAD", "SPOOL", self.name,
                                        self.cmd_KMMS_SPOOL_UNLOAD,
                                        desc=self.cmd_KMMS_SPOOL_UNLOAD_help)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

    def get_status(self, eventtime):
        filament_status = self.filament_switch.get_status(eventtime)

        return {
            'status': self.status,
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

        if not is_resend:
            self._log_debug("move cmd %.3f", value)

        if value >= 0.:
            self.status = self.STATUS_LOADING if value > 0 else self.STATUS_IDLE
            self.unload_pin.set_pwm(print_time, 0., cycle_time)
            self.load_pin.set_pwm(print_time, value, cycle_time)
        else:
            self.status = self.STATUS_UNLOADING
            self.load_pin.set_pwm(print_time, 0., cycle_time)
            self.unload_pin.set_pwm(print_time, abs(value), cycle_time)

        self.last_value = value
        self.last_cycle_time = cycle_time
        self.last_print_time = print_time

        if self.resend_interval and self.resend_timer is None:
            self.resend_timer = self.reactor.register_timer(
                self._resend_current_val, self.reactor.NOW)

    def stop(self):
        # Nothing to do, just ensure there is no pending state
        if self.last_value == 0.:
            self._reset_state()
            return

        self._log_info("stop and release")

        prev_unloading = self.last_value < 0.

        # Wait for spool to settle
        self.set_pin(self.toolhead.print_time, 0.)
        self.toolhead.dwell(0.1)
        if prev_unloading:
            # Move forward tiny bit
            self.set_pin(self.toolhead.print_time, self.load_power)
            self.toolhead.dwell(2 * self.release_pulse)
            # Wait
            self.set_pin(self.toolhead.print_time, 0.)
            self.toolhead.dwell(0.1)
        # Gear release pulse
        self.set_pin(self.toolhead.print_time, -self.release_pulse_power)
        self.toolhead.dwell(self.release_pulse)
        # Stop
        self.set_pin(self.toolhead.print_time, 0.)

        # Done
        result = self.reactor.monotonic() - self.last_start if self.last_start is not None else None
        if result is not None:
            self.last_duration = result
        self._resolve_state(result)

    def load_spool(self, print_time, wait=True):
        self._reset_state()

        eventtime = self.reactor.monotonic()
        status = self.get_status(eventtime)

        completion = self._trigger_completion = self.reactor.completion()

        if not status['filament_detected']:
            # No filament detected
            self._resolve_state(None)
        else:
            # Load
            self._log_info("loading")
            self.last_start = eventtime
            self.set_pin(print_time, self.load_power)
            # Timeout
            timeout_wake_time = eventtime + self.timeout
            self._timeout_timer = self.reactor.register_timer(self._handle_timeout, timeout_wake_time)
            # Wait
            if wait:
                completion.wait(timeout_wake_time + 0.3)

        return completion

    def unload_spool(self, print_time, wait=True):
        self._reset_state()

        eventtime = self.reactor.monotonic()
        status = self.get_status(eventtime)

        completion = self._trigger_completion = self.reactor.completion()

        if not status['filament_detected']:
            # No filament detected
            self._resolve_state(None)
        else:
            # Unload
            self._log_info("unloading")
            self.last_start = eventtime
            self.set_pin(print_time, -self.unload_power)
            # Timeout
            timeout_wake_time = eventtime + self.timeout
            self._timeout_timer = self.reactor.register_timer(self._handle_timeout, timeout_wake_time)
            # Wait
            if wait:
                completion.wait(timeout_wake_time + 0.3)

        return completion

    def _handle_timeout(self, eventtime):
        self._log_info("timeout detected during operation")
        self.gcode.respond_info("Timeout detected on spool %s during %s" % (self.name, self.status.lower()), log=False)

        self.printer.send_event('kmms_spool:timeout', eventtime, self.name)
        self.toolhead.register_lookahead_callback(
            lambda print_time: self.stop())

        return self.reactor.NEVER

    def _handle_runout(self, eventtime, name):
        if name != self.name:
            return

        self.gcode.respond_info("Runout detected on spool %s when %s" % (self.name, self.status.lower()), log=False)
        self.toolhead.register_lookahead_callback(
            lambda print_time: self.stop())

    def _resolve_state(self, result):
        if self._trigger_completion is not None and not self._trigger_completion.test():
            self._trigger_completion.complete(result)
            self._trigger_completion = None

    def _reset_state(self):
        if self._timeout_timer is not None:
            self.reactor.unregister_timer(self._timeout_timer)
            self._timeout_timer = None

        self._resolve_state(None)
        self.last_load_start = self.last_start = None

        self.set_pin(self.toolhead.print_time, 0.)

    # Commands

    cmd_SET_PIN_help = ("Control of the motor, use negative value to unload. "
                        "Low-level command, doesn't do any sanity checks.")

    def cmd_SET_PIN(self, gcmd):
        value = gcmd.get_float('VALUE', minval=-1., maxval=1.)
        cycle_time = gcmd.get_float('CYCLE_TIME', self.default_cycle_time,
                                    above=0., maxval=MAX_SCHEDULE_TIME)
        self.toolhead.register_lookahead_callback(
            lambda print_time: self.set_pin(print_time, value, cycle_time))

    cmd_KMMS_SPOOL_STOP_help = "Stop and release the gears. Low-level command, doesn't do any sanity checks."

    def cmd_KMMS_SPOOL_STOP(self, gcmd):
        self.toolhead.register_lookahead_callback(
            lambda print_time: self.stop())

    cmd_KMMS_SPOOL_UNLOAD_help = "Unload given spool. Low-level command, doesn't do any sanity checks."

    def cmd_KMMS_SPOOL_UNLOAD(self, gcmd):
        wait = bool(gcmd.get_int('WAIT', 1, minval=0))

        self.toolhead.register_lookahead_callback(
            lambda print_time: self.unload_spool(print_time, wait)
        )

    cmd_KMMS_SPOOL_LOAD_help = "Load given spool. Low-level command, doesn't do any sanity checks."

    def cmd_KMMS_SPOOL_LOAD(self, gcmd):
        wait = bool(gcmd.get_int('WAIT', 1, minval=0))

        self.toolhead.register_lookahead_callback(
            lambda print_time: self.load_spool(print_time, wait)
        )

    # Helpers

    def _log_info(self, msg, *args, **kwargs):
        args = (self.reactor.monotonic(), self.name) + args
        logging.info("KMMS %.6f: Spool %s " + msg, *args, **kwargs)

    def _log_debug(self, msg, *args, **kwargs):
        args = (self.reactor.monotonic(), self.name) + args
        logging.debug("KMMS %.6f: Spool %s " + msg, *args, **kwargs)

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
        custom_helper = SpoolRunoutHelper(config.getsection(section))
        fs.runout_helper = custom_helper
        fs.get_status = custom_helper.get_status

        return fs


class SpoolRunoutHelper:
    # Based on
    # https://github.com/moggieuk/Happy-Hare/blob/63565368c072bff2673a7a97469fb7663f62ee12/extras/mmu_sensors.py

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.name = config.get_name().split()[-1]
        self.min_event_systime = self.reactor.NEVER
        self.event_delay = 1.  # Time between generated events
        self.filament_present = False
        self.sensor_enabled = True

        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.runout_gcode = gcode_macro.load_template(config, 'runout_gcode', '')
        self.insert_gcode = gcode_macro.load_template(config, 'insert_gcode', '')

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
        self.printer.send_event('kmms_spool:insert', eventtime, self.name)
        self._exec_gcode(self.insert_gcode)

    def _runout_event_handler(self, eventtime):
        self.printer.send_event('kmms_spool:runout', eventtime, self.name)
        self._exec_gcode(self.runout_gcode)

    def _exec_gcode(self, template):
        try:
            self.gcode.run_script(template.render())
        except Exception:
            logging.exception("Script running error")
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
            logging.info("KMMS %.6f: Spool %s insert event detected", eventtime, self.name)
            self.reactor.register_callback(self._insert_event_handler)
        else:  # Runout detected
            self.min_event_systime = self.reactor.NEVER
            logging.info("KMMS %.6f: Spool %s runout event detected", eventtime, self.name)
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
