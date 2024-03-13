# KMMS
#
# Copyright (C) 2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

import extras.filament_switch_sensor
from . import kmms_filament_switch_sensor

ADC_REPORT_TIME = 0.015
ADC_SAMPLE_TIME = 0.001
ADC_SAMPLE_COUNT = 6
TOLERANCE = 0.01


class KmmsBackPressureSensor(extras.filament_switch_sensor.SwitchSensor):
    # noinspection PyMissingConstructor
    def __init__(self, config):
        # NOTE we inherit SwitchSensor, but we don't call its constructor at all

        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.full_name = config.get_name()
        self.name = config.get_name().split()[-1]

        # Runout handler
        self.runout_helper = kmms_filament_switch_sensor.EventsRunoutHelper(config, "back_pressure_%s" % self.name)
        self.min_event_systime = self.reactor.NEVER

        # Read config
        self.min = config.getfloat('min', minval=0, maxval=1)
        self.target = config.getfloat('target', minval=0, maxval=1)
        self.last_value = self.last_pressure = .0

        adc_report_time = config.getfloat('adc_report_time', ADC_REPORT_TIME, above=0.)
        adc_sample_time = config.getfloat('adc_sample_time', ADC_SAMPLE_TIME, above=0.)
        adc_sample_count = config.getint('adc_sample_count', ADC_SAMPLE_COUNT, min=1)

        ppins = self.printer.lookup_object('pins')
        self.mcu_adc = ppins.setup_pin('adc', config.get('adc'))
        self.mcu_adc.setup_minmax(adc_sample_time, adc_sample_count)
        self.mcu_adc.setup_adc_callback(adc_report_time, self.adc_callback)

        # Register events and commands
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        self.gcode.register_mux_command("SET_BACK_PRESSURE", "SENSOR", self.name,
                                        self.cmd_SET_BACK_PRESSURE, desc=self.cmd_SET_BACK_PRESSURE_help)

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.

    def _pressure_event_handler(self, eventtime):
        self._exec_event('kmms:backpressure', eventtime, self.full_name, self.last_pressure)

    def _exec_event(self, event, *params):
        self.logger.debug('Sending event %s', event)
        self.printer.send_event(event, *params)

    def adc_callback(self, read_time, read_value):
        eventtime = self.mcu_adc.get_mcu().print_time_to_clock(read_time)
        self.last_value = read_value
        pressure = read_value - self.target

        if eventtime < self.min_event_systime:
            return

        if self.runout_helper.sensor_enabled:
            self.logger.debug('%.1f: adc=%.3f pressure=%.3f', eventtime, self.last_value, pressure)
        self.runout_helper.note_filament_present(read_value >= self.min)

        if abs(pressure - self.last_pressure) >= TOLERANCE:
            self.last_pressure = pressure
            if self.runout_helper.sensor_enabled:
                self.reactor.register_callback(self._pressure_event_handler)

    def get_status(self, eventtime):
        return self.runout_helper.get_status(eventtime) | {
            'min': round(self.min, 3),
            'target': round(self.target, 3),
            'last_value': round(self.last_value, 3),
            'pressure': round(self.last_pressure, 3)
        }

    cmd_SET_BACK_PRESSURE_help = "Configure back-pressure sensor"

    def cmd_SET_BACK_PRESSURE(self, gcmd):
        self.min = max(0, min(1, gcmd.get_float('MIN', self.min)))
        self.target = max(0, min(1, gcmd.get_float('TARGET', self.target)))
        self.runout_helper.sensor_enabled = gcmd.get_int('ENABLE', self.runout_helper.sensor_enabled)
        self.runout_helper.runout_pause = gcmd.get_int('PAUSE_ON_RUNOUT', self.runout_helper.runout_pause)

        status = ["{}={}".format(k.upper(), v) for k, v in self.get_status(self.reactor.monotonic())]
        gcmd.respond_info("Back-pressure sensor %s: %s" % (self.name, status))


def load_config_prefix(config):
    obj = KmmsBackPressureSensor(config)
    # Register as a filament_switch_sensor as well, to be displayed in UI
    config.get_printer().add_object("filament_switch_sensor %s" % obj.runout_helper.name, obj)
    return obj
