# KMMS
#
# Based on https://github.com/moggieuk/Happy-Hare/blob/36435646f8eac82377ce1cf153c66aa2b79fbd0b/extras/mmu_sensors.py
#
# Copyright (C) 2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

import extras.filament_switch_sensor
from configfile import ConfigWrapper
from klippy import Printer
from reactor import Reactor


class CustomRunoutHelper:
    printer: Printer
    reactor: Reactor

    def __init__(self, config: ConfigWrapper):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.full_name = config.get_name()
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.printer.load_object(config, 'pause_resume')
        self.toolhead = None

        # Read config
        unsupported = [opt for opt in ['insert_gcode', 'runout_gcode', 'pause_on_runout', 'pause_delay', 'event_delay']
                       if config.get(opt, None)]
        if unsupported:
            raise self.printer.config_error(
                '%s options %s are not supported' % (self.full_name, ','.join(unsupported),))

        # Internal state
        self.min_event_systime = self.reactor.NEVER
        self.filament_present = False
        self.sensor_enabled = True

        # Register commands and event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        # We are going to replace previous runout_helper mux commands with ours
        _replace_mux_command(self.gcode,
                             "QUERY_FILAMENT_SENSOR", "SENSOR", self.name,
                             self.cmd_QUERY_FILAMENT_SENSOR, desc=self.cmd_QUERY_FILAMENT_SENSOR_help)

        _replace_mux_command(self.gcode,
                             "SET_FILAMENT_SENSOR", "SENSOR", self.name,
                             self.cmd_SET_FILAMENT_SENSOR, desc=self.cmd_SET_FILAMENT_SENSOR_help)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.min_event_systime = self.reactor.monotonic() + 2.  # Time to wait before first events are processed

    def _runout_event_handler(self, eventtime):
        self._exec_event('kmms:filament_runout', eventtime, self.full_name)

    def _insert_event_handler(self, eventtime):
        self._exec_event('kmms:filament_insert', eventtime, self.full_name)

    def _exec_event(self, event, *params):
        self.logger.debug('Sending event %s', event)
        self.printer.send_event(event, *params)

    def note_filament_present(self, is_filament_present):
        if is_filament_present == self.filament_present:
            return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()

        # Ignore if disabled
        if not self.sensor_enabled:
            return

        # Perform filament action associated with status change (if any)
        if is_filament_present:
            # insert detected
            self.logger.info("Filament Sensor %s: insert event detected, Time %.2f", self.name, eventtime)
            self.reactor.register_callback(self._insert_event_handler)
        else:
            # runout detected
            self.logger.info("Filament Sensor %s: runout event detected, Time %.2f", self.name, eventtime)
            self.reactor.register_callback(self._runout_event_handler)

    def get_status(self, eventtime):
        return {
            'filament_detected': bool(self.filament_present),
            'enabled': bool(self.sensor_enabled),
        }

    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"

    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "Filament Sensor %s: filament detected" % self.name
        else:
            msg = "Filament Sensor %s: filament not detected" % self.name
        gcmd.respond_info(msg)

    cmd_SET_FILAMENT_SENSOR_help = "Sets the filament sensor on/off"

    def cmd_SET_FILAMENT_SENSOR(self, gcmd):
        self.sensor_enabled = gcmd.get_int("ENABLE", 1)


def _replace_mux_command(gcode, cmd, key, value, func, desc=None):
    # Remove existing, if it exists
    prev = gcode.mux_commands.get(cmd)
    if prev:
        prev_key, prev_values = prev
        if prev_key == key:
            del prev_values[value]

    # Register new
    gcode.register_mux_command(cmd, key, value, func, desc=desc)


def runout_helper_attach(obj, config):
    obj.runout_helper = CustomRunoutHelper(config)
    obj.get_status = obj.runout_helper.get_status
    obj.full_name = obj.runout_helper.full_name
    obj.name = obj.runout_helper.name
    return obj


def load_config_prefix(config):
    obj = extras.filament_switch_sensor.SwitchSensor(config)

    # noinspection PyTypeChecker
    return runout_helper_attach(obj, config)
