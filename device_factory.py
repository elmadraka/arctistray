from arctis_device import ArctisDevice
from devices import arctis_7_plus as protocol

def get_protocol():
    return protocol

def create_device():
  return ArctisDevice(protocol)

