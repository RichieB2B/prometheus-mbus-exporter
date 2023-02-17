#!/usr/bin/env python3

import argparse
import logging
import lxml
import multiprocessing
import os
import subprocess
import time
import untangle
import yaml

from prometheus_client import start_http_server, Counter, Metric, REGISTRY
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

class ExporterProcess(multiprocessing.Process):

    def __init__(self, queue, location=None, port=None, address='::'):
        multiprocessing.Process.__init__(self)
        self.exit = multiprocessing.Event()
        self.location = location
        self.xml = ''
        self.queue = queue
        self.port = port
        self.address = address
        logging.info('ExporterProcess: init complete')
        return

    def run(self):
        logging.info('ExporterProcess: run')
        start_http_server(port=self.port, addr=self.address)
        REGISTRY.register(self)
        while not self.exit.is_set():
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                return

    def shutdown(self):
        logging.info("ExporterProcess: Shutdown initiated")
        self.exit.set()

    def get_xml_for_device(self):
        # always retrieve the latest queue item
        logging.debug('get_xml_for_device called')
        while not self.queue.empty():
            self.xml = self.queue.get()
            logging.debug('Popped queue item: "%s"', self.xml)
        return self.xml

    def parseMeterDataRecord(self, r):
        """
        <DataRecord id="0">
            <Function>Instantaneous value</Function>
            <StorageNumber>0</StorageNumber>
            <Unit>Energy (kWh)</Unit>
            <Value>1022</Value>
            <Timestamp>2023-02-12T08:45:37Z</Timestamp>
        </DataRecord>
        """
        return {
            'Function': r.Function.cdata,
            'StorageNumber': r.StorageNumber.cdata,
            'Unit': r.Unit.cdata,
            'Value': r.Value.cdata,
            'Timestamp': r.Timestamp.cdata,
        }


    def collect(self):
        xml = self.get_xml_for_device()
        try:
            obj = untangle.parse(xml.decode('utf-8'))
        except:
            yield CounterMetricFamily('kamstrup_energy_kwh', 'Energy in kWh', labels=['location'])
            return
        meterdata = []

        for data in obj.MBusData.DataRecord:
            if int(data['id']) in config['mbus']['record_ids']:
                meterdata.append(self.parseMeterDataRecord(data))

        for data in meterdata:
            if data['Function'] != 'Instantaneous value':
                continue

            # Normalize values
            unit = data['Unit'].replace('m m^3/h','l/h')
            if '(m ' in unit:
                value = float(data['Value']) / 1000
            elif '(1e-2 ' in unit:
                value = float(data['Value']) / 100
            elif '(1e-1 ' in unit:
                value = float(data['Value']) / 10
            elif '(10 ' in unit:
                value = int(data['Value']) * 10
            elif '(100 ' in unit:
                value = int(data['Value']) * 100
            else:
                value = int(data['Value'])

            # Extract actual unit: Volume (1e-2  m^3) -> m^3
            u_help = unit.split('(')[-1].split(' ')[-1].split(')')[0]
            u = u_help.lower().replace('^','').replace('/','_')
            t_help = data['Unit'].split(' (')[0]
            t = t_help.lower().replace(' ','_')
            if 'temperature' in t:
                t = t.replace('temperature','').strip('_')
                c = GaugeMetricFamily('kamstrup_temperature_celcius', 'Temperature in Celsius', labels=['type', 'location'])
                c.add_metric([t, self.location], value)
            elif any(gauge.lower() in unit.lower() for gauge in config['mbus']['gauges']):
                c = GaugeMetricFamily(f'kamstrup_{t}_{u}', f'{t_help} in {u_help}', labels=['location'])
                c.add_metric([self.location], value)
            else:
                c = CounterMetricFamily(f'kamstrup_{t}_{u}', f'{t_help} in {u_help}', labels=['location'])
                c.add_metric([self.location], value)

            logging.debug(f"Added {t_help} in {u_help} value {value}")
            yield c


class CollectorProcess(multiprocessing.Process):

    def __init__(self, queue, device=None, meter_id=None, baud_rate=None):
        multiprocessing.Process.__init__(self)
        self.exit = multiprocessing.Event()
        self.baud_rate = baud_rate
        self.device = device
        self.meter_id = meter_id
        self.xml = ''
        self.queue = queue
        logging.info('CollectorProcess: init complete')

    def run(self):
        logging.info('CollectorProcess: run')
        while not self.exit.is_set():
            self.retrieve_xml_for_device()
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                return

    def shutdown(self):
        logging.info("CollectorProcess: Shutdown initiated")
        self.exit.set()

    def retrieve_xml_for_device(self):
        try:
            self.xml = subprocess.Popen('mbus-serial-request-data -b {:d} {:s} {:d}'.
                format(self.baud_rate, self.device, self.meter_id),
                shell=True,
                stdout=subprocess.PIPE).stdout.read()
            logging.debug(self.xml)
            self.queue.put(self.xml)
        except Exception:
            pass

def read_yaml(config_file):
    config_name = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        config_file)
    if not os.path.isfile(config_name):
        logging.error('Config file %s not found.', config_name)
        return None
    with open(config_name, "r") as yaml_file:
        cfg = yaml.load(yaml_file, Loader=yaml.FullLoader)
    logging.debug('Read config file: %s', cfg)
    return cfg

def main():
    global config

    parser = argparse.ArgumentParser(
        description='M-Bus power meter Prometheus exporter.')

    parser.add_argument('-c', '--config', default='prometheus-mbus-exporter.yml', help='Exporter config file')
    parser.add_argument('-v', '--verbose',
                        dest='verbose', action='store_true', default=False,
                        help='Increase logging verbosity.')
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    logging.info('Starting up...')
    config = read_yaml(args.config)
    if not config:
        logging.error('Unable to load configuration, exitting.')
        return

    queue = multiprocessing.Queue()
    collector_process = CollectorProcess(
        queue,
        device=config['mbus']['device'],
        meter_id=config['mbus']['meter_id'],
        baud_rate=config['mbus']['baud_rate'])
    exporter_process = ExporterProcess(
        queue,
        location=config['exporter']['location'],
        port=config['exporter']['port'],
        address=config['exporter']['address'])
    collector_process.start()
    exporter_process.start()
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            collector_process.shutdown()
            exporter_process.shutdown()
            return

if __name__ == "__main__":
    main()
