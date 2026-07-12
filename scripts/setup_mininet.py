#!/usr/bin/python3
from mininet.net import Mininet
from mininet.node import Controller, RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info

def customTopology():
    net = Mininet(controller=RemoteController, link=TCLink, switch=OVSSwitch)

    c0 = net.addController('c0', controller=RemoteController, ip='192.168.10.10', port=6633)
    s1 = net.addSwitch('s1')
    s2 = net.addSwitch('s2')
    h1 = net.addHost('h1')
    net.addLink(s1, h1)
    h2 = net.addHost('h2')
    net.addLink(s1, h2)
    h3 = net.addHost('h3')
    net.addLink(s2, h3)
    h4 = net.addHost('h4')
    net.addLink(s2, h4)
    net.addLink(s1, s2)
    s1.start([c0])
    s2.start([c0])
    net.build()
    c0.start()
    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    customTopology()
