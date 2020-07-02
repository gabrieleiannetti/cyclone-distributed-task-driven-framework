#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2020 Gabriele Iannetti <g.iannetti@gsi.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#


import configparser
import operator
import logging
import signal
import random
import time
import sys
import os

from datetime import datetime
from enum import Enum, unique
from multiprocessing import Process

from ctrl.critical_section import CriticalSection
from globals import LOCAL_MODE
from lfs.lfs_utils import LFSUtils
from msg.base_message import BaseMessage
from task.ost_migrate_task import OstMigrateTask
from task.empty_task import EmptyTask


class LustreOstMigrateItem:

    def __init__(self, ost, filename):

        self.ost = ost
        self.filename = filename


@unique
class OSTState(Enum):

    READY = 1
    LOCKED = 2
    BLOCKED = 3
    PENDING_LOCK = 4


class LustreOstFileMigrationTaskGenerator(Process):

    def __init__(self,
                 task_queue,
                 lock_task_queue,
                 result_queue,
                 lock_result_queue,
                 config_file):

        super(LustreOstFileMigrationTaskGenerator, self).__init__()

        self.task_queue = task_queue
        self.lock_task_queue = lock_task_queue

        self.result_queue = result_queue
        self.lock_result_queue = lock_result_queue

        config = configparser.ConfigParser()
        config.read_file(open(config_file))

        if not LOCAL_MODE:
            self.lfs_utils = LFSUtils("/usr/bin/lfs")
            self.lfs_path = config.get('lustre', 'fs_path')

        ost_targets = config.get('lustre.migration', 'ost_targets')
        self.ost_target_list = ost_targets.strip().split(",")

        self.input_dir = config.get('lustre.migration', 'input_dir')

        self.ost_fill_threshold = config.getint('lustre.migration', 'ost_fill_threshold')

        self.ost_source_cache_dict = dict()

        # TODO: Create class for managing state and capacity for OSTs.
        self.ost_source_state_dict = dict()
        self.ost_target_state_dict = dict()

        self.ost_fill_level_dict = dict()

        self.run_flag = False

    def run(self):

        try:

            self.run_flag = True

            signal.signal(signal.SIGTERM, self._signal_handler_terminate)
            signal.siginterrupt(signal.SIGTERM, True)

            logging.info("%s started!" % self.__class__.__name__)

            self._update_ost_fill_level_dict()
            self._init_ost_target_state_dict()
            self._process_input_files()

            threshold_update_fill_level = 900
            next_time_update_fill_level = int(time.time()) + threshold_update_fill_level

            threshold_reload_files = 900
            next_time_reload_files = int(time.time()) + threshold_reload_files

            threshold_print_caches = 900
            next_time_print_caches = int(time.time()) + threshold_print_caches

            while self.run_flag:

                try:

                    for source_ost, ost_cache in self.ost_source_cache_dict.items():

                        if self.ost_source_state_dict[source_ost] == OSTState.READY:

                            if len(ost_cache):

                                for target_ost, target_state in self.ost_target_state_dict.items():

                                    if target_state == OSTState.READY:

                                        item = ost_cache.pop()

                                        if LOCAL_MODE:
                                            task = EmptyTask()
                                        else:
                                            task = OstMigrateTask(source_ost, target_ost, item.filename)

                                        task.tid = f"{source_ost}:{target_ost}"

                                        logging.debug("Pushing task with TID to task queue: %s" % task.tid)

                                        with CriticalSection(self.lock_task_queue):
                                            self.task_queue.push(task)

                                        self.ost_source_state_dict[source_ost] = OSTState.BLOCKED
                                        self.ost_target_state_dict[target_ost] = OSTState.BLOCKED

                                        break

                    while not self.result_queue.is_empty():

                        with CriticalSection(self.lock_result_queue):

                            finished_tid = self.result_queue.pop()

                            logging.debug("Popped TID from result queue: %s " % finished_tid)

                            source_ost, target_ost = finished_tid.split(":")

                            self._update_ost_state_dict(source_ost, self.ost_source_state_dict)
                            self._update_ost_state_dict(target_ost, self.ost_target_state_dict)

                    last_run_time = int(time.time())

                    if last_run_time >= next_time_update_fill_level:

                        next_time_update_fill_level = last_run_time + threshold_update_fill_level

                        logging.info("###### OST Fill Level Update ######")

                        start_time = datetime.now()
                        self._update_ost_fill_level_dict()
                        elapsed_time = datetime.now() - start_time

                        logging.info("Elapsed time: %s - Number of OSTs: %s"
                                     % (elapsed_time, len(self.ost_fill_level_dict)))

                        if logging.root.level <= logging.DEBUG:

                            for ost, fill_level in self.ost_fill_level_dict.items():
                                logging.debug("OST: %s - Fill Level: %s" % (ost, fill_level))

                        for ost in self.ost_source_state_dict.keys():
                            self._update_ost_source_state_dict(ost)

                        for ost in self.ost_target_state_dict.keys():
                            self._update_ost_target_state_dict(ost)

                    if last_run_time >= next_time_reload_files:

                        next_time_reload_files = last_run_time + threshold_reload_files

                        logging.info("###### Loading Input Files ######")

                        self._process_input_files()

                    if last_run_time >= next_time_print_caches:

                        next_time_print_caches = last_run_time + threshold_print_caches

                        logging.info("###### OST Cache Sizes ######")

                        ost_caches_keys = self.ost_source_cache_dict.keys()

                        if len(ost_caches_keys):

                            for source_ost in sorted(ost_caches_keys):
                                logging.info("OST: %s - Size: %s"
                                             % (source_ost, len(self.ost_source_cache_dict[source_ost])))

                        else:
                            logging.info("No OST caches available!")

                    # TODO: adaptive sleep... ???
                    time.sleep(0.001)

                except InterruptedError:
                    logging.error("Caught InterruptedError exception.")

        except Exception:

            exc_info = sys.exc_info()
            exc_value = exc_info[1]
            exc_tb = exc_info[2]

            filename = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]

            logging.error("Exception in %s (line: %s): %s"
                          % (filename, exc_tb.tb_lineno, exc_value))

            logging.info("%s exited!" % self.__class__.__name__)

            sys.exit(1)

        logging.info("%s finished!" % self.__class__.__name__)

        sys.exit(0)

    def _signal_handler_terminate(self, signum, frame):
        # pylint: disable=unused-argument

        self.run_flag = False

        msg = f"{self.__class__.__name__} retrieved signal to terminate."
        logging.debug(msg)
        raise InterruptedError(msg)

    def _process_input_files(self):

        file_counter = 0

        files = os.listdir(self.input_dir)

        for file in files:

            if file.endswith(".input"):

                file_path = self.input_dir + os.path.sep + file

                self._load_input_file(file_path)

                os.renames(file_path, file_path + ".done")

                file_counter += 1

        if file_counter:
            self._allocate_ost_source_caches()

        logging.info("Count of processed input files: %s" % file_counter)

    def _load_input_file(self, file_path):

        with open(file_path, mode="r", encoding="UTF-8") as file:

            loaded_counter = 0
            skipped_counter = 0

            for line in file:

                stripped_line = line.strip()

                if BaseMessage.field_separator in stripped_line:
                    logging.warning("Skipped line: %s" % line)
                    skipped_counter += 1
                    continue

                try:

                    ost, filename = stripped_line.split()
                    migrate_item = LustreOstMigrateItem(ost, filename)

                    if ost not in self.ost_source_cache_dict:
                        self.ost_source_cache_dict[ost] = list()

                    self.ost_source_cache_dict[ost].append(migrate_item)

                    loaded_counter += 1

                except ValueError as error:
                    logging.warning("Skipped line: %s (%s)" % (line, error))
                    skipped_counter += 1

            logging.info("Loaded input file: %s - Loaded: %s - Skipped: %s"
                         % (file_path, loaded_counter, skipped_counter))

    def _allocate_ost_source_caches(self):

        del_ost_source_list = None

        for ost, cache in self.ost_source_cache_dict.items():

            if len(cache):

                if not (ost in self.ost_source_state_dict):
                    self._update_ost_source_state_dict(ost)

            else:

                if self.ost_source_state_dict[ost] == OSTState.READY \
                        or self.ost_source_state_dict[ost] == OSTState.LOCKED:

                    if not del_ost_source_list:
                        del_ost_source_list = list()

                    del_ost_source_list.append(ost)

        if del_ost_source_list:

            for ost in del_ost_source_list:

                del self.ost_source_cache_dict[ost]
                del self.ost_source_state_dict[ost]

    def _init_ost_target_state_dict(self):

        for ost in self.ost_target_list:
            self._update_ost_target_state_dict(ost)

    def _update_ost_fill_level_dict(self):

        if LOCAL_MODE:

            self.ost_fill_level_dict.clear()

            for i in range(10):

                ost_idx = str(i)
                fill_level = random.randint(40, 60)

                self.ost_fill_level_dict[ost_idx] = fill_level

        else:
            self.ost_fill_level_dict = \
                self.lfs_utils.retrieve_ost_fill_level(self.lfs_path)

    def _update_ost_source_state_dict(self, ost):
        self._update_ost_state_dict(ost, self.ost_source_state_dict, operator.gt)

    def _update_ost_target_state_dict(self, ost):
        self._update_ost_state_dict(ost, self.ost_target_state_dict, operator.lt)

    def _update_ost_state_dict(self, ost, ost_state_dict, operator_func=None):

        if operator_func:

            if not (ost in self.ost_fill_level_dict):
                raise RuntimeError("OST not found in ost_fill_level_dict: %s" % ost)

            fill_level = self.ost_fill_level_dict[ost]

            # operator_func = operator.lt or operator.gt
            if operator_func(fill_level, self.ost_fill_threshold):

                if ost in ost_state_dict:

                    if ost_state_dict[ost] == OSTState.LOCKED:
                        ost_state_dict[ost] = OSTState.READY

                else:
                    ost_state_dict[ost] = OSTState.READY

            else:

                if ost in ost_state_dict:

                    if ost_state_dict[ost] == OSTState.READY:
                        ost_state_dict[ost] = OSTState.LOCKED
                    elif ost_state_dict[ost] == OSTState.BLOCKED:
                        ost_state_dict[ost] = OSTState.PENDING_LOCK

                else:
                    ost_state_dict[ost] = OSTState.LOCKED

        else:

            if ost_state_dict[ost] == OSTState.BLOCKED:
                ost_state_dict[ost] = OSTState.READY
            elif ost_state_dict[ost] == OSTState.PENDING_LOCK:
                ost_state_dict[ost] = OSTState.LOCKED
            else:
                raise RuntimeError("Inconsistency in OST state dictionaries found!")
