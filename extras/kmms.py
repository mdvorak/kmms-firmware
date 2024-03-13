# KMMS
#
# Copyright (C) 2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from typing import Any

import reactor
from gcode import GCodeDispatch
from klippy import Printer
from reactor import Reactor, ReactorCompletion
from toolhead import ToolHead


class KmmsError(Exception):
    pass


class KmmsObject:
    name: str

    NONE = 0
    SENSOR = 1
    EXTRUDER = 2
    SYNCING_EXTRUDER = 4
    BACKPRESSURE = 8 | SENSOR

    def __init__(self, obj):
        self.obj = obj
        self.name = getattr(obj, 'full_name', None) or obj.name
        self.get_status = obj.get_status

        # Build flags
        self.flags = self.NONE
        if 'filament_detected' in obj.get_status(reactor.Reactor.NEVER):
            self.flags |= self.SENSOR
        if hasattr(obj, 'move'):
            self.flags |= self.EXTRUDER
        if hasattr(obj, 'sync_to_extruder'):
            self.flags |= self.SYNCING_EXTRUDER

    def has_flag(self, flag: int):
        return flag & self.flags

    def filament_detected(self, eventtime):
        status = self.get_status(eventtime)
        return status['filament_detected'] if ('enabled' not in status or status['enabled']) else None


class KmmsPath:
    objects: list[KmmsObject]
    printer: Printer

    def __init__(self, printer, name):
        self.logger = logging.getLogger('kmms_path.%s' % name)
        self.printer = printer
        self.name = name
        self.objects = []

    def add_object(self, obj):
        wrapper = KmmsObject(obj)
        self.logger.info('Adding object %s', wrapper.name)
        self.objects.append(wrapper)

    def lookup_object(self, name):
        self.add_object(self.printer.lookup_object(name))

    def find_position(self, eventtime) -> (int, Any):
        self.logger.debug('Finding current position')
        result = (-1, None)
        for i, obj in enumerate(self.objects):
            filament_detected = obj.filament_detected(eventtime) if obj.has_flag(KmmsObject.SENSOR) else None
            if filament_detected:
                result = (i, obj)
            elif filament_detected is not None:
                # Stop on first empty sensor - this skips components that does not track filament
                break
        self.logger.info('Found position at %d', result[0])
        return result

    def find_next(self, flag: int, start=0, stop=None) -> (int, Any):
        self.logger.debug('Finding next %d from %d to %s', flag, start, stop)
        for i, obj in enumerate(self.objects[start:stop]):
            if obj.has_flag(flag):
                return start + i, obj
        return -1, None

    def find_all(self, flag: int, start=0, stop=None) -> list[(int, KmmsObject)]:
        self.logger.debug('Finding all %d from %d to %s', flag, start, stop)
        return [(i, obj) for i, obj in enumerate(self.objects[start:stop]) if obj.has_flag(flag)]

    def find_last(self, flag: int, start: int, stop=0) -> (int, Any):
        # TODO debug log
        self.logger.info('Finding last %d from %d to %s', flag, start, stop)
        for i in reversed(range(stop, start)):
            obj = self.objects[i]
            if obj.has_flag(flag):
                return start + i, obj
        return -1, None

    def __getitem__(self, pos):
        return self.objects[pos]

    def __len__(self):
        return len(self.objects)


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


class Kmms:
    printer: Printer
    reactor: Reactor
    gcode: GCodeDispatch
    toolhead: ToolHead
    paths: list[KmmsPath]
    extruders: list[KmmsObject]

    def __init__(self, config):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.endstop = KmmsVirtualEndstop(self.printer)

        # Read configuration
        self.max_velocity = config.getfloat('max_velocity', above=0.)
        self.max_accel = config.getfloat('max_accel', above=0.)

        # Load paths
        self.path = KmmsPath(self.printer, 'spool_0')
        self.paths = [self.path]

        # Register event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("kmms:filament_insert", self._handle_filament_runout)
        self.printer.register_event_handler("kmms:filament_runout", self._handle_filament_insert)

        self.gcode.register_command("KMMS_PRELOAD", self.cmd_KMMS_PRELOAD)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        self.path.lookup_object("kmms_extruder hub")
        self.path.lookup_object("kmms_filament_switch_sensor hub")
        self.path.lookup_object("kmms_filament_switch_sensor toolhead")
        self.path.lookup_object("extruder")
        self.path.lookup_object("kmms_filament_switch_sensor extruder")

    def _handle_filament_insert(self, eventtime, full_name):
        pass

    def _handle_filament_runout(self, eventtime, full_name):
        pass

    def move_to_toolhead(self):
        path = self.path
        eventtime = self.reactor.monotonic()

        if len(path) < 1:
            raise self.printer.command_error("No filament is selected")

        # Find all extruders
        extruders = path.find_all(KmmsObject.EXTRUDER)
        if len(extruders) < 1:
            raise self.printer.config_error("Path does not have toolhead extruder configured")

        # Get toolhead extruder
        toolhead_pos, toolhead_extruder = extruders.pop()

        # Find current position
        pos, _ = path.find_position(eventtime)
        if pos < 0:
            raise KmmsError("It seems to be empty")
        if pos >= toolhead_pos:
            self.gcode.respond_info("%s is already at toolhead" % path.name)
            return False

        # Desync all known extruders
        self.logger.info('Desync extruders')
        self.printer.send_event('kmms:desync_extruders')

        # Activate last extruder before toolhead
        if len(extruders) < 1:
            raise self.printer.config_error("Path does not have any extruders configured")
        drive_extruder_pos, drive_extruder = extruders.pop()
        self.activate_extruder(drive_extruder.name)

        # Sync all remaining extruders
        for _, extruder in extruders:
            self.sync_to_extruder(extruder.name, drive_extruder.name)

        # Find last sensor before toolhead
        toolhead_sensor_pos, toolhead_sensor = path.find_last(KmmsObject.SENSOR, toolhead_pos)

        # Move to toolhead
        # TODO this can be handled with static distances later
        if toolhead_sensor is None:
            raise KmmsError("KMMS: %s does not have any sensors before toolhead configured" % path.name)

        # Find backpressure sensors between last toolhead and drive extruders
        backpressure_names = [obj.name for _, obj in
                              path.find_all(KmmsObject.BACKPRESSURE, drive_extruder_pos, toolhead_sensor_pos)]

        drip_completion = self.endstop.start([toolhead_sensor.name] + backpressure_names)
        self.gcode.respond_info("KMMS: Moving to '%s'" % toolhead_sensor.name)
        self.toolhead.drip_move(self.new_pos(100), 300, drip_completion)  # TODO speed and pos
        return True

    def new_pos(self, e: float):
        pos = self.toolhead.get_position()
        pos[3] += e
        return pos

    def activate_extruder(self, extruder_name: str):
        self.logger.info('Activating extruder %s', extruder_name)
        self.gcode.run_script_from_command('ACTIVATE_EXTRUDER EXTRUDER="%s"' % extruder_name)

    def sync_to_extruder(self, extruder_name: str, motion_queue: str):
        self.logger.info('Syncing extruder %s to %s', extruder_name, motion_queue)
        self.gcode.run_script_from_command(
            'SYNC_EXTRUDER_MOTION EXTRUDER="%s" MOTION_QUEUE="%s"' % (extruder_name, motion_queue))

    def cmd_KMMS_PRELOAD(self, gcmd):
        try:
            self.move_to_toolhead()
        except KmmsError as e:
            self.gcode.respond_info('KMMS Error: %s', e)


def load_config(config):
    return Kmms(config)
