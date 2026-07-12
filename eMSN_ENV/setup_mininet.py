#!/usr/bin/python3
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info

# NOTE:
# - Switch protocol pinned to OpenFlow10 to match ryu ofproto_v1_0
# - Each domain i uses controller at 192.168.(10+i).10, port (6633+i)

def customTopology(c, s):
    net = Mininet(controller=RemoteController, link=TCLink, switch=OVSSwitch, build=False)

    # Controllers
    controllers = []
    for i in range(c):
        cid = 'c%d' % i
        ip = '192.168.%d.10' % (10 + i)
        port = 6633 + i
        ctrl = RemoteController(cid, ip=ip, port=port)
        net.addController(ctrl)
        controllers.append(ctrl)

    # Switches + hosts
    switch_idx = 1
    host_idx = 1
    switches = []
    for i in range(c):
        for j in range(s):
            sw = net.addSwitch('s%d' % switch_idx, protocols='OpenFlow10')
            h1 = net.addHost('h%d' % host_idx); host_idx += 1
            h2 = net.addHost('h%d' % host_idx); host_idx += 1
            net.addLink(sw, h1)
            net.addLink(sw, h2)
            switches.append(sw)
            switch_idx += 1

    # Chain switches linearly (across all sets) for LLDP discovery
    for k in range(len(switches) - 1):
        net.addLink(switches[k], switches[k+1])

    net.build()

    # Start controllers
    for ctrl in controllers:
        ctrl.start()

    # Assign switches to their domain controllers
    idx = 0
    for i in range(c):
        for j in range(s):
            switches[idx].start([controllers[i]])
            idx += 1

    # LLDP to controller for topology (mirror of your bash version)
    for sw in switches:
        sw.dpctl('add-flow', 'dl_type=0x88cc,actions=CONTROLLER')

    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    import os
    c = int(os.getenv('CSETS', '1'))
    s = int(os.getenv('SPER', '1'))
    customTopology(c, s)
