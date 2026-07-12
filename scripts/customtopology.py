from mininet.net import Mininet
from mininet.node import Controller, RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink

def customTopology():
    net = Mininet(controller=RemoteController, link=TCLink, switch=OVSSwitch)

    # Add controllers
    c0 = net.addController('c0', controller=RemoteController, ip='192.168.10.15', port=6633)
    c1 = net.addController('c1', controller=RemoteController, ip='127.0.0.1', port=6634)

    # Add switches
    s1 = net.addSwitch('s1')
    s2 = net.addSwitch('s2')
    s3 = net.addSwitch('s3')
    s4 = net.addSwitch('s4')

    # Add hosts and connect to switches
    h1 = net.addHost('h1')
    h2 = net.addHost('h2')
    net.addLink(h1, s1)
    net.addLink(h2, s1)

    h3 = net.addHost('h3')
    h4 = net.addHost('h4')
    net.addLink(h3, s2)
    net.addLink(h4, s2)

    h5 = net.addHost('h5')
    h6 = net.addHost('h6')
    net.addLink(h5, s3)
    net.addLink(h6, s3)

    h7 = net.addHost('h7')
    h8 = net.addHost('h8')
    net.addLink(h7, s4)
    net.addLink(h8, s4)

    # Connect switches together
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s3, s4)

    # Assign controllers to switches
    s1.start([c0])
    s2.start([c0])
    s3.start([c1])
    s4.start([c1])

    # Start the network
    net.build()
    c0.start()
    c1.start()

    # Add flow rules to handle LLDP packets
    for switch in [s1, s2, s3, s4]:
        switch.dpctl('add-flow', 'dl_type=0x88cc,actions=CONTROLLER')

    # Start the CLI
    CLI(net)

    # Stop the network
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    customTopology()

