import asyncio

import network
import timer
from enum import Enum
import logging
import datetime
import time


_SHORT = datetime.timedelta(seconds=4)
_LONG = datetime.timedelta(seconds=8)
_TIMER_LONG = datetime.timedelta(seconds=20)
_MARGIN = 2
_REPEAT = _MARGIN * (_LONG / _SHORT)


class DHT(network.Network, timer.Timer):
    class State(Enum):
        START = 1
        MASTER = 2
        SLAVE = 3

    def update_peer_list(self):

        for (_, timer) in self._context.heartbeat_timer.items():
            timer.cancel()
        self._context.heartbeat_timer.clear()
        self._context.timestamp = time.time()
        self._context.data_counter_dict.clear()
        message = {
            "type": "leader_is_here",
            "uuid": self.uuid,
            "timestamp": self._context.timestamp,
            "peer_count": len(self._context.peer_list) + 1,
        }
        logging.info("leader_is_here_sent")
        self.send_message(message, (network.NETWORK_BROADCAST_ADDR, network.NETWORK_PORT))

        index = 0
        for (uuid, addr) in self._context.peer_list:
            self._context.heartbeat_timer[uuid] = \
                self.async_trigger(lambda: self.master_heartbeat_timeout(uuid), _TIMER_LONG)
            index += 1
            message = {
                "type": "peer_list",
                "uuid": self.uuid,
                "timestamp": self._context.timestamp,
                "peer_index": index,
                "peer_uuid": uuid,
                "peer_addr": addr,
            }
            self.send_message(message, (network.NETWORK_BROADCAST_ADDR, network.NETWORK_PORT))

    def message_arrived(self, message, addr):
        if message["uuid"] == self.uuid:
            return
        logging.debug("Message received from {addr}, {message}".format(addr=addr, message=message))

        if message["type"] == "hello":
            logging.info("hello message arrived")
            if self._state == self.State.START:
                self._context.messages.append((message, addr))
            elif self._state == self.State.MASTER:
                if not (message["uuid"], addr) in self._context.peer_list:
                    self._context.peer_list.append((message["uuid"], addr))
                    self._context.peer_list.sort(reverse=True)
                    self.update_peer_list()
                    self.master_peer_list_updated()
        elif message["type"] == "heartbeat_ping":
            logging.info("!!!!!PING!!!!!")
            message = {
                "type": "heartbeat_pong",
                "uuid": self.uuid,
                "timestamp": time.time(),
            }
            logging.info("mydata:{data}".format(data=self._context.data))
            self.send_message(message, addr)
        elif message["type"] == "heartbeat_pong":
            logging.info("!!!!!PONG!!!!!")
            if self._state == self.State.MASTER:
                client_uuid = message["uuid"]
                if client_uuid in self._context.heartbeat_timer:
                    prev = self._context.heartbeat_timer[client_uuid]
                    prev.cancel()
                    self._context.heartbeat_timer[client_uuid] = \
                        self.async_trigger(lambda: self.master_heartbeat_timeout(client_uuid), _TIMER_LONG)
            elif self._state == self.State.SLAVE:
                master_uuid = message["uuid"]
                if self._context.master_uuid == master_uuid:
                    self._context.heartbeat_timer.cancel()
                    self._context.heartbeat_timer = self.async_trigger(self.slave_heartbeat_timeout, _TIMER_LONG)
        elif message["type"] == "leader_is_here":
            logging.info("leader_is_here")
            tmp = None
            if self._state == self.State.SLAVE:
                tmp = self._context.data_counter
            if self._state == self.State.START or \
                    (self._state == self.State.SLAVE and self._context.master_timestamp < message["timestamp"]):
                self._context.cancel()
                self._state = self.State.SLAVE
                self._context = self.SlaveContext()
                self._context.master_uuid = message["uuid"]
                self._context.master_addr = addr
                self._context.peer_count = int(message["peer_count"])
                self._context.master_timestamp = message["timestamp"]
                if tmp:
                    self._context.data_counter = tmp
                message = {
                    "type": "data_counter",
                    "uuid": self.uuid,
                    "data_counter": self._context.data_counter,
                }
                self.send_message(message, self._context.master_addr)
                asyncio.ensure_future(self.slave(), loop=self._loop)
                pass
        elif message["type"] == "data_counter":
            if self._state == self.State.MASTER:
                self._context.data_counter_dict[message["uuid"]] = message["data_counter"]
        elif message["type"] == "peer_list":
            logging.info("peer_list1 self._state = {state1} is it equals SLAVE? then peer_list2 should show up.".format(state1=self._state))
            if self._state == self.State.SLAVE:
                logging.info("peer_list2 self._context.master_uuid = {context_uuid} message[uuid] = {uuid} are they the same? then peer_list3 should show up.".format(context_uuid=self._context.master_uuid, uuid=message["uuid"]))
                if self._context.master_uuid == message["uuid"]:
                    logging.info("peer_list3")
                    self._context.peer_index[message["peer_index"]] = (message["peer_uuid"], message["peer_addr"])

                    if (len(self._context.peer_index) + 1) == self._context.peer_count:
                        logging.info("peer_list4")
                        self._context.peer_list = []
                        for i in range(1, self._context.peer_count):
                            self._context.peer_list.append(self._context.peer_index[i])
                        self.slave_peer_list_updated()
        elif message["type"] == "new_leader_election":
            if self._context.heartbeat_send_job is not None:
                self._context.heartbeat_send_job.cancel()
            self._context.cancel()
            self._state = self.State.START
            self._context = self.StartContext()
            asyncio.ensure_future(self.start(), loop=self._loop)
        elif message["type"] == "you_are_rejected":
            if self._context.heartbeat_send_job is not None:
                self._context.heartbeat_send_job.cancel()
            self._context.cancel()
            self._state = self.State.START
            self._context = self.StartContext()
            asyncio.ensure_future(self.start(), loop=self._loop)
        elif message["type"] == "get":
            logging.info("Client request: get")
            if self._state == self.State.SLAVE:
                _message = {
                    "type": "get_relayed",
                    "uuid": self.uuid,
                    "cli_addr": addr,
                    "key": message["key"],
                }
                self.send_message(_message, self._context.master_addr)
            elif self._state == self.State.MASTER:
                logging.info("{key} and {keys} and {bool}".format(key=message["key"], keys=self._context.data.keys(), bool=(message["key"] in self._context.data.keys())))
                if message["key"] in self._context.data.keys():
                    _message = {
                        "type": "get_success",
                        "uuid": self.uuid,
                        "key": message["key"],
                        "value": self._context.data[message["key"]],
                    }
                    self.send_message(_message, addr)
                else:
                    for (uuid, addr) in self._context.peer_list:
                        _message = {
                            "type": "get_ask",
                            "uuid": self.uuid,
                            "cli_addr": addr,
                            "key": message["key"],
                        }
                        self.send_message(_message, addr)
        elif message["type"] == "get_relayed":
            if self._state == self.State.MASTER:
                if message["key"] in self._context.data.keys():
                    _message = {
                        "type": "get_success",
                        "uuid": self.uuid,
                        "key": message["key"],
                        "value": self._context.data[message["key"]],
                    }
                    self.send_message(_message, tuple(message["cli_addr"]))
                else:
                    for (uuid, addr) in self._context.peer_list:
                        _message = {
                            "type": "get_ask",
                            "uuid": self.uuid,
                            "cli_addr": message["cli_addr"],
                            "key": message["key"],
                        }
                        self.send_message(_message, addr)
        elif message["type"] == "get_ask":
            if self._state == self.State.SLAVE:
                if message["key"] in self._context.data.keys():
                    _message = {
                        "type": "get_success",
                        "uuid": self.uuid,
                        "key": message["key"],
                        "value": self._context.data[message["key"]],
                    }
                    self.send_message(_message, tuple(message["cli_addr"]))
        elif message["type"] == "put":
            logging.info("Client request: put")
            if self._state == self.State.SLAVE:
                _message = {
                    "type": "put_relayed",
                    "uuid": self.uuid,
                    "cli_addr": addr,
                    "key": message["key"],
                    "value": message["value"],
                }
                self.send_message(_message, self._context.master_addr)
            elif self._state == self.State.MASTER:
                if not self._context.data_counter_dict:
                    self._context.data[message["key"]] = message["value"]
                    self._context.data_counter += 1
                    _message = {
                        "type": "put_success",
                        "uuid": self.uuid,
                    }
                    self.send_message(_message, addr)
                else:
                    min_uuid = min(self._context.data_counter_dict, key=self._context.data_counter_dict.get)
                    if self._context.data_counter < self._context.data_counter_dict[min_uuid]:
                        self._context.data[message["key"]] = message["value"]
                        self._context.data_counter += 1
                        _message = {
                            "type": "put_success",
                            "uuid": self.uuid,
                        }
                        self.send_message(_message, addr)
                    else:
                        tmp = addr
                        for (uuid, addr) in self._context.peer_list:
                            if min_uuid == uuid:
                                _message = {
                                    "type": "put_final",
                                    "uuid": self.uuid,
                                    "cli_addr": tmp,
                                    "key": message["key"],
                                    "value": message["value"],
                                }
                                self.send_message(_message, addr)
                                self._context.data_counter_dict[uuid] += 1
        elif message["type"] == "put_relayed":
            logging.info("put_relayed")
            if self._state == self.State.MASTER:
                if not self._context.data_counter_dict:
                    self._context.data[message["key"]] = message["value"]
                    self._context.data_counter += 1
                    _message = {
                        "type": "put_success",
                        "uuid": self.uuid,
                    }
                    self.send_message(_message, tuple(message["cli_addr"]))
                else:
                    min_uuid = min(self._context.data_counter_dict, key=self._context.data_counter_dict.get)
                    if self._context.data_counter < self._context.data_counter_dict[min_uuid]:
                        self._context.data[message["key"]] = message["value"]
                        self._context.data_counter += 1
                        _message = {
                            "type": "put_success",
                            "uuid": self.uuid,
                        }
                        self.send_message(_message, tuple(message["cli_addr"]))
                    else:
                        tmp = message["cli_addr"]
                        for (uuid, addr) in self._context.peer_list:
                            if min_uuid == uuid:
                                _message = {
                                    "type": "put_final",
                                    "uuid": self.uuid,
                                    "cli_addr": tmp,
                                    "key": message["key"],
                                    "value": message["value"],
                                }
                                self.send_message(_message, addr)
                                self._context.data_counter_dict[uuid] += 1
        elif message["type"] == "put_final":
            logging.info("put_final")
            if self._state == self.State.SLAVE:
                self._context.data[message["key"]] = message["value"]
                tmp = tuple(message["cli_addr"])
                _message = {
                    "type": "put_success",
                    "uuid": self.uuid,
                }
                self.send_message(_message, tmp)

        elif message["type"] == "delete":
            logging.info("Client request: delete")
            pass

    def master_peer_list_updated(self):
        logging.info("Peer list updated: I'm MASTER with {peers} peers".format(peers=len(self._context.peer_list)))
        for (uuid, addr) in self._context.peer_list:
            logging.info("Peer list updated: PEER[{peer}]".format(peer=str((uuid, addr))))

    def slave_peer_list_updated(self):
        logging.info("Peer list updated: MASTER[{master}] with {peers} peers".format(
            master=str((self._context.master_uuid, self._context.master_addr)), peers=len(self._context.peer_list)))
        for (uuid, addr) in self._context.peer_list:
            logging.info("Peer list updated: PEER[{peer}]".format(peer=str((uuid,addr))))

    async def slave_heartbeat_timeout(self):
        message = {
            "type": "new_leader_election",
            "uuid": self.uuid,
        }
        self.send_message(message, (network.NETWORK_BROADCAST_ADDR, network.NETWORK_PORT))
        if self._context.heartbeat_send_job is not None:
            self._context.heartbeat_send_job.cancel()
        self._context.cancel()
        self._state = self.State.START
        self._context = self.StartContext()
        logging.info("slave_timeout")
        asyncio.ensure_future(self.start(), loop=self._loop)

    async def master_heartbeat_timeout(self, client_uuid):
        client = None
        message = {
            "type": "you_are_rejected",
            "uuid": self.uuid,
        }
        for (uuid, addr) in self._context.peer_list:
            if uuid == client_uuid:
                client = (uuid, addr)
                self.send_message(message, addr)
        self._context.peer_list.remove(client)
        self.update_peer_list()
        logging.info("master_timeout")
        self.master_peer_list_updated()

    class StartContext:
        def __init__(self):
            self.hello_job = None
            self.timeout_job = None
            self.messages = []

        def cancel(self):
            if self.hello_job is not None:
                self.hello_job.cancel()
            if self.timeout_job is not None:
                self.timeout_job.cancel()
            pass

    class MasterContext:
        def __init__(self):
            self.peer_list = []
            self.timestamp = time.time()
            self.heartbeat_send_job = None
            self.heartbeat_timer = {}
            self.data_counter_dict = {}
            self.data_counter = 0
            self.data = {}

        def cancel(self):
            if self.heartbeat_send_job is not None:
                self.heartbeat_send_job.cancel()
            for (_, timer) in self.heartbeat_timer.items():
                timer.cancel()
            pass

    class SlaveContext:
        def __init__(self):
            self.peer_list = []
            self.peer_index = {}
            self.peer_count = 0
            self.master_addr = None
            self.master_uuid = None
            self.master_timestamp = None
            self.heartbeat_send_job = None
            self.heartbeat_timer = None
            self.data_counter = 0
            self.data = {}

        def cancel(self):
            if self.heartbeat_send_job is not None:
                self.heartbeat_send_job.cancel()
            if self.heartbeat_timer is not None:
                self.heartbeat_timer.cancel()
            pass

    async def master(self):
        async def heartbeat_send():
            for (_, addr) in self._context.peer_list:
                message = {
                    "type": "heartbeat_ping",
                    "uuid": self.uuid,
                    "timestamp": time.time(),
                }
                logging.info("master_heartbeat")
                self.send_message(message, addr)
        self._context.heartbeat_send_job = self.async_period(heartbeat_send, _SHORT)
        pass

    async def slave(self):
        async def heartbeat_send():
            message = {
                "type": "heartbeat_ping",
                "uuid": self.uuid,
                "timestamp": time.time(),
            }
            logging.info("slave_heartbeat")
            self.send_message(message, self._context.master_addr)

        self._context.heartbeat_timer = self.async_trigger(self.slave_heartbeat_timeout, _TIMER_LONG)
        self._context.heartbeat_send_job = self.async_period(heartbeat_send, _SHORT)
        pass

    async def start(self):
        self._context = self.StartContext()
        async def hello():
            logging.info("hello job entered")
            message = {
                "type": "hello",
                "uuid": self.uuid,
            }
            logging.debug("Sending HELLO message")
            self.send_message(message, (network.NETWORK_BROADCAST_ADDR, network.NETWORK_PORT))

        async def timeout():
            self._context.hello_job.cancel()
            logging.info("Cannot find any existing leader.")
            if len(self._context.messages) == 0:
                logging.info("Cannot find any peer. I am the leader.")
                self._state = self.State.MASTER
                self._context = self.MasterContext()
                asyncio.ensure_future(self.master(), loop=self._loop)
            else:
                max_val = self.uuid
                max_addr = None
                unique_addr = set()
                for (message, addr) in self._context.messages:
                    if message["uuid"] > max_val:
                        max_val = message["uuid"]
                        max_addr = addr
                    if message["uuid"] != self.uuid:
                        unique_addr.add((message["uuid"], addr))
                if max_addr is None:
                    #I am the leader
                    sorted_list = list(unique_addr)
                    sorted_list.sort(reverse=True)
                    self._context = self.MasterContext()
                    self._state = self.State.MASTER
                    self._context.peer_list = sorted_list
                    asyncio.ensure_future(self.master(), loop=self._loop)
                    logging.info("I am the leader of {peers} peers".format(peers=len(sorted_list)))
                else:
                    #I am the slave
                    self._context.cancel()
                    self._context = self.SlaveContext()
                    self._state = self.State.SLAVE
                    self._context.master_addr = max_addr
                    self._context.master_uuid = max_val
                    self._context.master_timestamp = -1
                    #asyncio.ensure_future(self.slave(), loop=self._loop)
                    logging.info("I am the slave of MASTER {master_addr}.".format(master_addr=max_addr))

            if self._state == self.State.MASTER:
                self.update_peer_list()
                logging.info("master_elected")
                self.master_peer_list_updated()

        self._context.hello_job = self.async_period(hello, _SHORT)
        self._context.timeout_job = self.async_trigger(timeout, _LONG)

        pass

    def __init__(self, loop):
        network.Network.__init__(self, loop)
        timer.Timer.__init__(self, loop)
        self._state = self.State.START
        self._loop = loop
        self._context = None

        import uuid
        self.uuid = str(uuid.uuid1())

        asyncio.ensure_future(self.start(), loop=self._loop)
