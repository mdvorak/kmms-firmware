import logging
from typing import Any

from configfile import ConfigWrapper
from klippy import Printer
from reactor import Reactor


class KmmsObject:
    name: str

    def __init__(self, obj):
        self.obj = obj
        self.name = getattr(obj, 'full_name', None) or obj.name
        self.get_status = obj.get_status

        # Get fake status
        status = obj.get_status(Reactor.NEVER)

        # Build flags
        self.flags = KmmsPath.NONE
        if 'filament_detected' in status:
            self.flags |= KmmsPath.SENSOR
        if hasattr(obj, 'move'):
            self.flags |= KmmsPath.EXTRUDER
        if hasattr(obj, 'sync_to_extruder'):
            self.flags |= KmmsPath.SYNCING_EXTRUDER
        if isinstance(obj, KmmsPath):
            self.flags |= KmmsPath.PATH

    def has_flag(self, flag: int):
        return flag & self.flags

    def filament_detected(self, eventtime):
        status = self.get_status(eventtime)
        return status['filament_detected'] if ('enabled' not in status or status['enabled']) else None


class KmmsPath:
    KmmsObject = KmmsObject

    NONE = 0
    SENSOR = 1
    EXTRUDER = 2
    SYNCING_EXTRUDER = 4
    BACKPRESSURE = 8 | SENSOR
    PATH = 16

    objects: list[KmmsObject]
    printer: Printer

    def __init__(self, config: ConfigWrapper):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()

        self.full_name = config.get_name()
        self.name = config.get_name().split()[-1]

        self.path = list(filter(None, (p.strip() for p in config.getlist('path', sep='\n'))))
        self.objects = []

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        for obj_name in self.path:
            self.lookup_object(obj_name)

    def add_object(self, obj):
        wrapper = KmmsObject(obj)
        self.logger.info('Adding object %s', wrapper.name)
        self.objects.append(wrapper)

        # If obj is another path, explode it
        if isinstance(obj, KmmsPath):
            self.objects.extend(obj.objects)

    def lookup_object(self, name):
        self.add_object(self.printer.lookup_object(name.strip()))

    def find_position(self, eventtime) -> (int, Any):
        self.logger.debug('Finding current position')
        result = (-1, None)
        for i, obj in enumerate(self.objects):
            filament_detected = obj.filament_detected(eventtime) if obj.has_flag(self.SENSOR) else None
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
        self.logger.debug('Finding last %d from %d to %s', flag, start, stop)
        for i in reversed(range(stop, start)):
            obj = self.objects[i]
            if obj.has_flag(flag):
                return start + i, obj
        return -1, None

    def __getitem__(self, pos):
        return self.objects[pos]

    def __len__(self):
        return len(self.objects)


def load_config_prefix(config):
    return KmmsPath(config)


def load_config(config):
    return KmmsPath
