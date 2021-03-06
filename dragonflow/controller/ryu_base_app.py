# Copyright (c) 2015 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import time

from oslo_config import cfg
from oslo_log import log

from neutron.i18n import _LI

from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.controller import ofp_event
from ryu.controller.ofp_handler import OFPHandler
from ryu.ofproto import ofproto_v1_3

from dragonflow.controller.dispatcher import AppDispatcher


LOG = log.getLogger(__name__)


class RyuDFAdapter(OFPHandler):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    OF_AUTO_PORT_DESC_STATS_REQ_VER = 0x04

    def __init__(self, db_store=None):
        super(RyuDFAdapter, self).__init__(db_store=db_store)
        self.dispatcher = AppDispatcher('dragonflow.controller',
                cfg.CONF.df.apps_list)
        self.db_store = db_store
        self._datapath = None
        self.table_handlers = {}

    @property
    def datapath(self):
        return self._datapath

    def start(self):
        super(RyuDFAdapter, self).start()
        self.load(self, db_store=self.db_store)
        self.wait_until_ready()

    def load(self, *args, **kwargs):
        self.dispatcher.load(*args, **kwargs)

    def is_ready(self):
        return self.datapath is not None

    def wait_until_ready(self):
        while not self.is_ready():
            time.sleep(3)

    def register_table_handler(self, table_id, handler):
        assert table_id not in self.table_handlers
        self.table_handlers[table_id] = handler

    def unregister_table_handler(self, table_id, handler):
        self.table_handlers.pop(table_id, None)

    def notify_update_logical_switch(self, lswitch=None):
        self.dispatcher.dispatch('update_logical_switch', lswitch=lswitch)

    def notify_remove_logical_switch(self, lswitch=None):
        self.dispatcher.dispatch('remove_logical_switch', lswitch=lswitch)

    def notify_add_local_port(self, lport=None):
        self.dispatcher.dispatch('add_local_port', lport=lport)

    def notify_remove_local_port(self, lport=None):
        self.dispatcher.dispatch('remove_local_port', lport=lport)

    def notify_add_remote_port(self, lport=None):
        self.dispatcher.dispatch('add_remote_port', lport=lport)

    def notify_remove_remote_port(self, lport=None):
        self.dispatcher.dispatch('remove_remote_port', lport=lport)

    def notify_add_router_port(self,
            router=None, router_port=None, local_network_id=None):
        self.dispatcher.dispatch('add_router_port', router=router,
                router_port=router_port,
                local_network_id=local_network_id)

    def notify_remove_router_port(self,
            router_port=None, local_network_id=None):
        self.dispatcher.dispatch('remove_router_port',
                router_port=router_port,
                local_network_id=local_network_id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        # TODO(oanson) is there a better way to get the datapath?
        self._datapath = ev.msg.datapath
        super(RyuDFAdapter, self).switch_features_handler(ev)
        version = self.datapath.ofproto.OFP_VERSION
        if version < RyuDFAdapter.OF_AUTO_PORT_DESC_STATS_REQ_VER:
            # Otherwise, this is done automatically by OFPHandler
            self.send_port_desc_stats_request(self.datapath)
        self.dispatcher.dispatch('switch_features_handler', ev)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        msg = ev.msg
        reason = msg.reason
        port_no = msg.desc.port_no
        port_name = msg.desc.name

        ofproto = msg.datapath.ofproto
        if reason == ofproto.OFPPR_ADD:
            LOG.info(_LI("port added %s"), port_no)
            lport = self.db_store.get_local_port_by_name(port_name)
            if lport:
                lport.set_external_value('ofport', port_no)
                lport.set_external_value('is_local', True)
                self.notify_add_local_port(lport)
        elif reason == ofproto.OFPPR_DELETE:
            LOG.info(_LI("port deleted %s"), port_no)
            lport = self.db_store.get_local_port_by_name(port_name)
            if lport:
                self.notify_remove_local_port(lport)
                # Leave the last correct OF port number of this port
        elif reason == ofproto.OFPPR_MODIFY:
            LOG.info(_LI("port modified %s"), port_no)
            # TODO(oanson) Add notification
        else:
            LOG.info(_LI("Illeagal port state %(port_no)s %(reason)s")
                     % {'port_no': port_no, 'reason': reason})

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_stats_reply_handler(self, ev):
        self.dispatcher.dispatch('port_desc_stats_reply_handler', ev)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def OF_packet_in_handler(self, event):
        msg = event.msg
        table_id = msg.table_id
        if table_id in self.table_handlers:
            handler = self.table_handlers[table_id]
            handler(event)
        else:
            LOG.info(_LI("No handler for table id %s"), format(table_id))
