# KMMS
#
# Copyright (C) 2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

from gcode import GCodeDispatch
from klippy import Printer
from reactor import Reactor, ReactorCompletion
from toolhead import ToolHead


class Kmms:
    printer: Printer
    reactor: Reactor
    gcode: GCodeDispatch
    toolhead: ToolHead

    def __init__(self, config):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.endstop = KmmsVirtualEndstop(self.printer)

        # self.spools = self.joins = self.encoders = self.filament_switches = self.back_pressure_sensors = None
        # self.paths = dict()

        self.path = []

        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("kmms:filament_insert", self._handle_filament_runout)
        self.printer.register_event_handler("kmms:filament_runout", self._handle_filament_insert)

        self.gcode.register_command("_KMMS_MOVE_NEXT", self.cmd_KMMS_MOVE_NEXT)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        self.path = [
            self.printer.lookup_object("kmms_extruder hub_0"),
            self.printer.lookup_object("kmms_filament_switch_sensor hub_0"),
            self.printer.lookup_object("kmms_filament_switch_sensor toolhead"),
            self.printer.lookup_object("extruder"),
            self.printer.lookup_object("kmms_filament_switch_sensor extruder"),
        ]

    def _handle_filament_insert(self, eventtime, full_name):
        pass

    def _handle_filament_runout(self, eventtime, full_name):
        pass

    def find_filament_pos(self):
        eventtime = self.reactor.monotonic()
        pos = -1

        for i, obj in enumerate(self.path):
            status = obj.get_status(eventtime)
            if status['filament_detected'] is True:
                pos = i
            elif status['filament_detected'] is False:
                # Stop on first empty sensor - this skips components that does not track filament
                break

        return pos

    def move_next(self):
        if len(self.path) < 1:
            self.gcode.respond_info("KMMS: No filament path is selected")
            return False

        # Find current position
        pos = self.find_filament_pos()
        if pos < 0:
            self.gcode.respond_info("KMMS: %s seems to be empty" % self.path[0].full_name)
            return False

        # Find usable extruders
        extruders = self._find_active_extruders(pos)
        if not extruders:
            raise self.printer.config_error(
                "Cannot complete load sequence, there is no extruder before %s to move the filament" %
                self.path[pos].full_name)

        # Find target sensor
        next_sensor = self._find_sensor(pos + 1)
        if next_sensor is None:
            active_extruder = self.toolhead.get_extruder()
            if active_extruder in extruders:
                # Perform final move
                self.toolhead.move(self._new_pos(50))  # TODO speed and pos
                return False
            else:
                # Cannot complete load sequence
                raise self.printer.config_error(
                    "Cannot complete load sequence, there is no filament sensor after %s" % self.path[pos].full_name)

        # Perform move
        self._activate_extruders(extruders)
        drip_completion = self.endstop.start([next_sensor.full_name])

        # TODO test it!
        self.toolhead.drip_move(self._new_pos(100), 300, drip_completion)  # TODO speed and pos

        # Try moving next
        return True

    def _new_pos(self, e: float):
        pos = self.toolhead.get_position()
        pos[3] += e
        return pos

    def _find_active_extruders(self, pos):
        # Reverse iteration to zero
        extruders = []
        for i in range(pos, -1, -1):
            if hasattr(self.path[i], 'activate'):
                extruders.append(self.path[i])
        return extruders

    def _find_sensor(self, start_pos):
        eventtime = self.reactor.monotonic()
        # TODO handle disabled
        return next((obj for obj in self.path[start_pos:] if 'filament_detected' in obj.get_status(eventtime)), None)

    def _activate_extruders(self, extruders):
        # Activate extruders
        main_extruder = extruders[-1]
        main_extruder_name = main_extruder.full_name or main_extruder.name  # PrinterExtruder exposes only name attr
        synced_extruders = extruders[:-1]

        # Activate main
        prev_extruder = self.toolhead.get_extruder()
        if main_extruder is not prev_extruder:
            self.gcode.run_script("ACTIVATE_EXTRUDER EXTRUDER=%s" % main_extruder_name)

        # Sync the rest
        for extruder in synced_extruders:
            extruder.sync_to_extruder(main_extruder_name)

    def cmd_KMMS_MOVE_NEXT(self, gcmd):
        self.toolhead.register_lookahead_callback(lambda print_time: self.move_next())


class KmmsVirtualEndstop:
    reactor: Reactor

    def __init__(self, printer: Printer):
        self.reactor = printer.get_reactor()
        self.waiting = (self.reactor.completion(), [])

        printer.register_event_handler("kmms:filament_insert", self._handle_filament_insert)

    def start(self, names) -> ReactorCompletion:
        completion = self.reactor.completion()

        prev, _ = self.waiting
        self.waiting = (completion, list(names))

        # Make sure we don't leave anyone hanging
        if not prev.test():
            prev.complete(None)

        return completion

    def _handle_filament_insert(self, eventtime, full_name):
        completion, names = self.waiting
        if full_name in names and not completion.test():
            completion.complete(full_name)


def load_config(config):
    return Kmms(config)
