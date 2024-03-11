# KMMS spool config
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


class Spool(object):
    STATUS_IDLE = "Idle"
    STATUS_LOADING = "Loading"
    STATUS_UNLOADING = "Unloading"

    def __init__(self, config):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name().split()[-1]
        self.friendly_name = self.name.replace('_', ' ')
        self.toolhead = None

        # Motor PWM
        ppins = self.printer.lookup_object('pins')
        self.load_pin = ppins.setup_pin('pwm', config.get('load_pin'))
        self.unload_pin = ppins.setup_pin('pwm', config.get('unload_pin'))

        self.load_power = config.getfloat('load_power', 1., above=.0, maxval=1.)
        self.unload_power = config.getfloat('unload_power', 1., above=.0, maxval=1.)
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
        self.last_status = self.STATUS_IDLE
        self.last_duration = self.last_start = None
        self._trigger_completion = None
        self._timeout_timer = self.reactor.register_timer(self._handle_timeout, self.reactor.NEVER)

        # Register commands and handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        self.gcode.register_mux_command("SET_PIN", "PIN", self.name,
                                        self.cmd_SET_PIN,
                                        desc=self.cmd_SET_PIN_help)

        self.gcode.register_mux_command("_KMMS_SPOOL_STOP", "NAME", self.name,
                                        self.cmd_KMMS_SPOOL_STOP,
                                        desc=self.cmd_KMMS_SPOOL_STOP_help)

        self.gcode.register_mux_command("_KMMS_SPOOL_LOAD", "NAME", self.name,
                                        self.cmd_KMMS_SPOOL_LOAD,
                                        desc=self.cmd_KMMS_SPOOL_LOAD_help)

        self.gcode.register_mux_command("_KMMS_SPOOL_UNLOAD", "NAME", self.name,
                                        self.cmd_KMMS_SPOOL_UNLOAD,
                                        desc=self.cmd_KMMS_SPOOL_UNLOAD_help)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

    def get_status(self, eventtime):
        return {
            'status': self.last_status,
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
            self.logger.debug("%.1f: Move cmd %.3f", self.reactor.monotonic(), value)

        if value >= 0.:
            self._set_status(self.STATUS_LOADING if value > 0 else self.STATUS_IDLE)
            self.unload_pin.set_pwm(print_time, 0., cycle_time)
            self.load_pin.set_pwm(print_time, value, cycle_time)
        else:
            self._set_status(self.STATUS_UNLOADING)
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

        self.logger.info("%.1f: Stop and release", self.reactor.monotonic())

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
        completion = self._trigger_completion = self.reactor.completion()

        # Load
        self.logger.info("%.1f: Loading", eventtime)
        self.last_start = eventtime
        self.set_pin(print_time, self.load_power)
        # Timeout
        timeout_wake_time = eventtime + self.timeout
        self.reactor.update_timer(self._timeout_timer, timeout_wake_time)
        # Wait
        if wait:
            completion.wait(timeout_wake_time + 0.3)  # it should be always triggered before timeout is reached

        return completion

    def unload_spool(self, print_time, wait=True):
        self._reset_state()

        eventtime = self.reactor.monotonic()
        completion = self._trigger_completion = self.reactor.completion()

        # Unload
        self.logger.info("%.1f: Unloading", eventtime)
        self.last_start = eventtime
        self.set_pin(print_time, -self.unload_power)
        # Timeout
        timeout_wake_time = eventtime + self.timeout
        self.reactor.update_timer(self._timeout_timer, timeout_wake_time)
        # Wait
        if wait:
            completion.wait(timeout_wake_time + 0.3)  # it should be always triggered before timeout is reached

        return completion

    def _handle_timeout(self, eventtime):
        # Stop
        self.logger.info("%.1f: Timeout detected during operation", eventtime)
        self.toolhead.register_lookahead_callback(lambda print_time: self.stop())

        # Notify user
        self.gcode.respond_info("Timeout detected on %s while %s after %.1f s" %
                                (self.friendly_name, self.last_status.lower(), self.timeout))

        # Fire event for other handlers
        self.printer.send_event('kmms:spool_timeout', eventtime, self.name)
        return self.reactor.NEVER

    def _exec_event(self, event, *params):
        try:
            self.logger.debug('Sending event %s', event)
            self.printer.send_event(event, *params)
        except Exception:
            self.logger.exception("Error in %s event handler", event)

    def _resolve_state(self, result):
        self.set_pin(self.toolhead.print_time, 0.)

        self.reactor.update_timer(self._timeout_timer, self.reactor.NEVER)

        completion = self._trigger_completion
        self._trigger_completion = None
        if completion is not None and not completion.test():
            completion.complete(result)

    def _reset_state(self):
        self.last_start = None
        self._resolve_state(None)

    def _set_status(self, status):
        if status != self.last_status:
            self.last_status = status
            self.printer.send_event('kmms_spool:status', self.reactor.monotonic(), self.name, status)

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


def load_config_prefix(config):
    return Spool(config)
