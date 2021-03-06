import json
import os
import socket

import sys

import re

from l1_driver_resource_info import L1DriverResourceInfo
from l1_handler_interface import L1HandlerInterface


class PolatisRawConnection:
    def __init__(self, address, port, username, password, logger):
        self._address = address
        self._port = port
        self._username = username
        self._password = password
        self._logger = logger
        self._sock = None
        self._counter = 0
        self._switch_name = 'bad-switch-name'
        self.connect()

    def __del__(self):
        self.disconnect()

    def connect(self):
        self._logger.info('Connecting to %s...' % self._address)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self._address, self._port))
        self._counter = 0

        self.command('ACT - USER::%s:{counter}::%s;' % (self._username, self._password))

        s = self.command('RTRV-HDR:::{counter}:;')
        if '( nil )' in s:
            self._switch_name = ''
            self._logger.info('Switch name was "( nil )"')
        else:
            m = re.search(r'(\S+)', s)
            if m:
                self._switch_name = m.groups()[0]
                self._logger.info('Taking as switch name: "%s"' % self._switch_name)
            else:
                self._logger.info('Switch name regex not found: %s' % s)

        self._logger.info('Connected')

    def _write(self, s):
        while s:
            try:
                n = self._sock.send(s)
            except Exception as e:
                self._logger.info('Caught send failure %s; reconnecting and retrying' % str(e))
                self.reconnect()
                n = self._sock.send(s)
            self._logger.info('sent <<<%s>>>' % s[0:n])
            s = s[n:]

    def _read_until(self, regex):
        buf = ''
        while True:
            b = self._sock.recv(1024)
            self._logger.info('recv returned <<<%s>>>' % b)
            if b:
                buf += b
            if re.search(regex, buf):
                self._logger.info('read complete: <<<%s>>>' % buf)
                return buf
            if not b:
                raise Exception('End of recv stream without encountering termination pattern "%s"' % regex)

    def command(self, c):
        c = c.replace('{name}', self._switch_name)
        self._counter += 1
        c = c.replace('{counter}', str(self._counter))
        prompt = r'M\s+%d\s+([a-zA-Z ]+)[^;]*;' % self._counter

        self._write(c + '\n')
        rv = self._read_until(prompt)
        m = re.search(prompt, rv)
        status = m.groups()[0]
        if status != 'COMPLD':
            raise Exception('Error: Status "%s": %s' % (status, rv))
        return rv

    def disconnect(self):
        self._logger.info('Disconnecting...')
        try:
            self._sock.close()
        except:
            pass
        self._sock = None
        self._logger.info('Disconnected')

    def reconnect(self):
        self.disconnect()
        self.connect()


class PolatisL1Handler(L1HandlerInterface):
    def __init__(self, logger):
        self._logger = logger

    def _get_json_settings(self):
        try:
            with open(os.path.join(os.path.dirname(sys.argv[0]), 'polatis_python_runtime_configuration.json')) as f:
                o = json.loads(f.read())
        except Exception as e:
            self._logger.warn('Failed to read JSON config file: ' + str(e))
            o = {}

        port3082 = o.get("common_variable", {}).get("connection_port", 3082)

        islogical = o.get("driver_variable", {}).get("port_mode_logical_or_physical", "logical").lower() == 'logical'
        # For 256x256, must be a mapping from number in the range 1..256 to number in the range 257..512 
        portmap = o.get("driver_variable", {}).get("logical_port_pair_mapping", {})
        for k in list(portmap.keys()):
            if isinstance(k, str):
                portmap[int(k)] = int(portmap[k])
                del portmap[k]
        
        return port3082, islogical, portmap
        
    def login(self, address, username, password):
        """
        :param address: str
        :param username: str
        :param password: str
        :return: None
        """
        self._logger.info('Login called')
        port3082, logical, portmap = self._get_json_settings()
        
        self._connection = PolatisRawConnection(address, port3082, username, password, self._logger)

    def logout(self):
        """
        :return: None
        """
        self._logger.info('Logout called')
        self._connection.disconnect()
        self._connection = None

    def _getsize(self):
        _, islogical, _ = self._get_json_settings()

        psize = self._connection.command("RTRV-EQPT:{name}:SYSTEM:{counter}:::PARAMETER=SIZE;")
        m = re.search(r'SYSTEM:SIZE=(?P<a>\d+)x(?P<b>\d+)', psize)
        if m:
            size1 = int(m.groupdict()['a'])
            size2 = int(m.groupdict()['b'])

            if islogical:
                return size1
            else:
                return size1 + size2
        else:
            raise Exception('Unable to determine system size: %s' % psize)

    def get_resource_description(self, address):
        """
        :param address: str
        :return: L1DriverResourceInfo
        """
        self._logger.info('get_resource_description called')
        _, islogical, _ = self._get_json_settings()

        size = self._getsize()
        pserial = self._connection.command("RTRV-INV:{name}:OCS:{counter}:;")
        m = re.search(r'SN=(\w+)', pserial)
        if m:
            serial = m.groups()[0]
        else:
            self._logger.warn('Failed to extract serial number: %s' % pserial)
            serial = '-1'

        sw = L1DriverResourceInfo('', address, 'L1 Optical Switch', 'Polatis', serial=serial)

        netype = self._connection.command('RTRV-NETYPE:{name}::{counter}:;')
        m = re.search(r'"(?P<vendor>.*),(?P<model>.*),(?P<type>.*),(?P<version>.*)"', netype)
        if not m:
            m = re.search(r'(?P<vendor>.*),(?P<model>.*),(?P<type>.*),(?P<version>.*)', netype)
        if m:
            sw.set_attribute('Vendor', m.groupdict()['vendor'])
            sw.set_attribute('Hardware Type', m.groupdict()['type'])
            sw.set_attribute('Version', m.groupdict()['version'])
            sw.set_attribute('Model', m.groupdict()['model'])
        else:
            self._logger.warn('Unable to parse system info: %s' % netype)

        portaddr2partneraddr = {}
        patch = self._connection.command("RTRV-PATCH:{name}::{counter}:;")
        for line in patch.split('\n'):
            line = line.strip()
            m = re.search(r'"(\d+),(\d+)"', line)
            if m:
                a = int(m.groups()[0])
                b = int(m.groups()[1])
                portaddr2partneraddr[a] = b
                portaddr2partneraddr[b] = a

        if islogical:
            portaddr2partneraddr_fixed = {}
            for p in portaddr2partneraddr:
                p2 = portaddr2partneraddr[p]
                if p > size:
                    p -= size
                    portaddr2partneraddr_fixed[p] = p2
                elif p2 > size:
                    p2 -= size
                    portaddr2partneraddr_fixed[p2] = p

            portaddr2partneraddr = portaddr2partneraddr_fixed

        portaddr2status = {}
        shutters = self._connection.command("RTRV-PORT-SHUTTER:{name}:1&&%d:{counter}:;" % size)
        for line in shutters.split('\n'):
            line = line.strip()
            m = re.search(r'"(\d+):(\S+)"', line)
            if m:
                portaddr2status[int(m.groups()[0])] = m.groups()[1]

        for portaddr in range(1, size+1):
            if portaddr in portaddr2partneraddr:
                mappath = '%s/%d' % (address, portaddr2partneraddr[portaddr])
            else:
                mappath = None
            p = L1DriverResourceInfo('Port %0.4d' % portaddr,
                                     '%s/%d' % (address, portaddr),
                                     'L1 Optical Switch Port',
                                     'Port Polatis',
                                     map_path=mappath,
                                     serial='%s.%d' % (serial, portaddr))
            p.set_attribute('State', 0 if portaddr2status.get(portaddr, 'open').lower() == 'open' else 1, typename='Lookup')
            p.set_attribute('Protocol Type', 0, typename='Lookup')
            sw.add_subresource(p)

        self._logger.info('get_resource_description returning xml: [[[' + sw.to_string() + ']]]')
        return sw

    def map_uni(self, src_port, dst_port):
        """
        :param src_port: str
        :param dst_port: str
        :return: None
        """
        self._logger.info('map_uni {} {}'.format(src_port, dst_port))
        _, islogical, portmap = self._get_json_settings()

        if islogical:
            src = int(src_port.split('/')[-1])
            dst = int(dst_port.split('/')[-1])
            # lower number must be the first in the command
            a = dst
            b = src
            if b in portmap:
                b = portmap[src]
            else:
                b += self._getsize()
            self._connection.command("ENT-PATCH:{name}:%d,%d:{counter}:;" % (a, b))
        else:
            raise Exception('map_uni not available in physical port mode')

    def map_bidi(self, src_port, dst_port, mapping_group_name):
        """
        :param src_port: str
        :param dst_port: str
        :param mapping_group_name: str
        :return: None
        """
        self._logger.info('map_bidi {} {} group={}'.format(src_port, dst_port, mapping_group_name))
        _, islogical, portmap = self._get_json_settings()

        src = int(src_port.split('/')[-1])
        dst = int(dst_port.split('/')[-1])

        if islogical:
            size = None
            for a, b in [(dst, src), (src, dst)]:
                if b in portmap:
                    b = portmap[b]
                else:
                    if not size:
                        size = self._getsize()
                    b += size
                # lower number must be the first in the command
                self._connection.command("ENT-PATCH:{name}:%d,%d:{counter}:;" % (a, b))
        else:
            self._connection.command("ENT-PATCH:{name}:%d,%d:{counter}:;" % (min(src, dst), max(src, dst)))

    def map_clear_to(self, src_port, dst_port):
        """
        :param src_port: str
        :param dst_port: str
        :return: None
        """
        self._logger.info('map_clear_to {} {}'.format(src_port, dst_port))
        _, islogical, portmap = self._get_json_settings()

        src = int(src_port.split('/')[-1])
        dst = int(dst_port.split('/')[-1])

        if islogical:
            self._connection.command("DLT-PATCH:{name}:%d:{counter}:;" % dst)
        else:
            self._connection.command("DLT-PATCH:{name}:%d:{counter}:;" % min(src, dst))

    def map_clear(self, src_port, dst_port):
        """
        :param src_port: str
        :param dst_port: str
        :return: None
        """
        self._logger.info('map_clear {} {}'.format(src_port, dst_port))
        _, islogical, portmap = self._get_json_settings()

        if islogical:
            self._logger.info('map_clear delegating to map_clear_to (1 of 2) {} {}'.format(src_port, dst_port))
            self.map_clear_to(src_port, dst_port)
            self._logger.info('map_clear delegating to map_clear_to (2 of 2) {} {}'.format(dst_port, src_port))
            self.map_clear_to(dst_port, src_port)
        else:
            self._logger.info('map_clear delegating to map_clear_to {} {}'.format(src_port, dst_port))
            self.map_clear_to(src_port, dst_port)

    def set_speed_manual(self, src_port, dst_port, speed, duplex):
        """
        :param src_port: str
        :param dst_port: str
        :param speed: str
        :param duplex: str
        :return: None
        """
        self._logger.info('set_speed_manual {} {} {} {}'.format(src_port, dst_port, speed, duplex))

    def set_state_id(self, state_id):
        """
        :param state_id: str
        :return: None
        """
        self._logger.info('set_state_id {}'.format(state_id))

    def get_attribute_value(self, address, attribute_name):
        """
        :param address: str
        :param attribute_name: str
        :return: str
        """
        self._logger.info('get_attribute_value {} {} -> "fakevalue"'.format(address, attribute_name))
        return 'fakevalue'

    def get_state_id(self):
        """
        :return: str
        """
        self._logger.info('get_state_id')
        return '-1'

