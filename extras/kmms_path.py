import logging
from typing import Any, Optional

from configfile import ConfigWrapper
from klippy import Printer
from reactor import Reactor


class KmmsPathItem:
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
        if 'pressure' in status:
            self.flags |= KmmsPath.BACKPRESSURE
        if hasattr(obj, 'move'):
            self.flags |= KmmsPath.EXTRUDER
        if hasattr(obj, 'sync_to_extruder'):
            self.flags |= KmmsPath.SYNCING_EXTRUDER
        if hasattr(obj, 'get_path_items'):
            self.flags |= KmmsPath.PATH

    def has_flag(self, flag: int) -> bool:
        return (flag & self.flags) == flag

    def filament_detected(self, eventtime) -> Optional[bool]:
        status = self.get_status(eventtime)
        return status['filament_detected'] if ('enabled' not in status or status['enabled']) else None

    def get_object(self):
        return self.obj

    def __str__(self):
        return self.name


class KmmsPath:
    KmmsPathItem = KmmsPathItem

    NONE = 0
    SENSOR = 1
    EXTRUDER = 2
    SYNCING_EXTRUDER = 4
    BACKPRESSURE = 8 | SENSOR
    PATH = 16

    printer: Printer
    _items: list[KmmsPathItem]

    def __init__(self, config: ConfigWrapper):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()

        self.full_name = config.get_name()
        self.name = config.get_name().split()[-1]

        self.path = list(filter(None, (p.strip() for p in config.getlist('path', sep='\n'))))
        self._items = []

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        for obj_name in self.path:
            self.lookup_object(obj_name)

    def add_object(self, obj):
        if obj is self:
            raise self.printer.config_error("Trying to add self to '%s'" % self.full_name)

        wrapper = KmmsPathItem(obj)
        self.logger.info('Adding object %s', wrapper.name)
        self._items.append(wrapper)

        # If obj is another path, explode it
        if wrapper.has_flag(self.PATH):
            self._items.extend(obj.get_path_items())

        # Validate
        used = set()
        for i in self.get_path_items():
            if i.get_object() in used:
                raise self.printer.config_error("'%s' has duplicate object '%s'" % (self.full_name, i.name))
            used.add(i.get_object())

    def lookup_object(self, name):
        self.add_object(self.printer.lookup_object(name.strip()))

    def get_path_items(self) -> list[KmmsPathItem]:
        return self._items

    def get_objects(self, flag=NONE, start=0, stop=None) -> list[object]:
        return [i.get_object() for i in self._items[start:stop] if i.has_flag(flag)]

    def find_path_position(self, eventtime) -> (int, Optional[KmmsPathItem]):
        self.logger.debug('Finding current position')
        result = (-1, None)
        for i, obj in enumerate(self._items):
            filament_detected = obj.filament_detected(eventtime) if obj.has_flag(self.SENSOR) else None
            if filament_detected:
                result = (i, obj)
            elif filament_detected is not None:
                # Stop on first empty sensor - this skips components that does not track filament
                break
        self.logger.info('Found position at %d', result[0])
        return result

    def find_path_items(self, flag: int, start=0, stop=None) -> list[(int, KmmsPathItem)]:
        self.logger.debug('Finding all %d from %d to %s', flag, start, stop)
        return [(start + i, obj) for i, obj in enumerate(self._items[start:stop]) if obj.has_flag(flag)]

    def find_path_next(self, flag: int, start=0, stop=None) -> (int, Optional[KmmsPathItem]):
        self.logger.debug('Finding next %d from %d to %s', flag, start, stop)
        for i, obj in enumerate(self._items[start:stop]):
            if obj.has_flag(flag):
                return start + i, obj
        return -1, None

    def find_path_last(self, flag: int, start: int, stop=0) -> (int, Optional[KmmsPathItem]):
        self.logger.debug('Finding last %d from %d to %s', flag, start, stop)
        for i in reversed(range(stop, start)):
            obj = self._items[i]
            if obj.has_flag(flag):
                return i, obj
        return -1, None

    def __getitem__(self, pos: int) -> Optional[KmmsPathItem]:
        return self._items[pos]

    def __len__(self):
        return len(self._items)


def load_config_prefix(config):
    return KmmsPath(config)


def load_config(config):
    return KmmsPath
