# KMMS
#
# Copyright (C) 2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

from . import kmms_filament_switch_sensor

ADC_REPORT_TIME = 0.200
ADC_SAMPLE_TIME = 0.03
ADC_SAMPLE_COUNT = 15
TOLERANCE = 0.01


class BackPressureSensor:
    def __init__(self, config):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.full_name = config.get_name()
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        # Read config
        self.min = config.getfloat('min', minval=0, maxval=1)
        self.target = config.getfloat('target', minval=0, maxval=1)
        self.last_value = self.last_pressure = .0
        self.sensor_enabled = True

        ppins = self.printer.lookup_object('pins')
        self.mcu_adc = ppins.setup_pin('adc', config.get('adc'))
        self.mcu_adc.setup_minmax(ADC_SAMPLE_TIME, ADC_SAMPLE_COUNT)
        self.mcu_adc.setup_adc_callback(ADC_REPORT_TIME, self.adc_callback)

        # Register events and commands
        self.gcode.register_mux_command("SET_BACK_PRESSURE", "SENSOR", self.name,
                                        self.cmd_SET_BACK_PRESSURE, desc=self.cmd_SET_BACK_PRESSURE_help)

    def _pressure_event_handler(self, eventtime):
        self._exec_event('kmms:backpressure', eventtime, self.name, self.last_pressure)

    def _exec_event(self, event, *params):
        try:
            self.logger.debug('Sending event %s', event)
            self.printer.send_event(event, *params)
        except Exception:
            self.logger.exception("Error in %s event handler", event)

    def adc_callback(self, read_time, read_value):
        eventtime = self.mcu_adc.get_mcu().print_time_to_clock(read_time)

        self.last_value = read_value
        pressure = read_value - self.target
        self.logger.debug('%.1f: adc=%.3f pressure=%.3f', eventtime, self.last_value, pressure)

        if abs(pressure - self.last_pressure) >= TOLERANCE:
            self.last_pressure = pressure
            if self.sensor_enabled:
                self.reactor.register_callback(self._pressure_event_handler)

    def get_status(self, eventtime):
        return {
            'enabled': bool(self.sensor_enabled),
            'min': round(self.min, 3),
            'target': round(self.target, 3),
            'last_value': round(self.last_value, 3),
            'pressure': round(self.last_pressure, 3),
        }

    cmd_SET_BACK_PRESSURE_help = "Configure back-pressure sensor"

    def cmd_SET_BACK_PRESSURE(self, gcmd):
        self.min = max(0, min(1, gcmd.get_float('MIN', self.min)))
        self.target = max(0, min(1, gcmd.get_float('TARGET', self.target)))
        self.sensor_enabled = gcmd.get_int('ENABLE', self.sensor_enabled)

        status = ["{}={}".format(k.upper(), v) for k, v in self.get_status(self.reactor.monotonic())]
        gcmd.respond_info("Back-pressure sensor %s: %s" % (self.name, status))


def load_config_prefix(config):
    return BackPressureSensor(config)
