#!/usr/bin/python3
import argparse, time, os, shutil, json, subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel

def run_cmd(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def build_net(c, s):
    net = Mininet(controller=RemoteController, link=TCLink, switch=OVSSwitch, cleanup=True)
    controllers = []
    for i in range(c):
        ctrl_ip = f"192.168.{10+i}.10"
        controllers.append(net.addController(f'c{i}', controller=RemoteController, ip=ctrl_ip, port=6633+i))
    sw_idx, host_idx = 1, 1
    for _ in range(c):
        for __ in range(s):
            sw = net.addSwitch(f's{sw_idx}')
            h1 = net.addHost(f'h{host_idx}'); net.addLink(sw,h1); host_idx += 1
            h2 = net.addHost(f'h{host_idx}'); net.addLink(sw,h2); host_idx += 1
            sw_idx += 1
    for i in range(1, sw_idx-1):
        net.addLink(f's{i}', f's{i+1}')
    net.build()
    for ctrl in controllers: ctrl.start()
    sw_ptr = 1
    for i in range(c):
        cl=[controllers[i]]
        for __ in range(s):
            net.get(f's{sw_ptr}').start(cl); sw_ptr+=1
    for i in range(1, sw_idx):
        net.get(f's{i}').dpctl('add-flow', 'dl_type=0x88cc,actions=CONTROLLER')
    return net, sw_idx-1, host_idx-1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--c', type=int, required=True)
    ap.add_argument('--s', type=int, required=True)
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--src', type=int, default=1)
    ap.add_argument('--dst', type=int, required=True)
    ap.add_argument('--block_url', required=True)
    args = ap.parse_args()
    setLogLevel('info')
    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    net, nsw, nhosts = build_net(args.c, args.s)

    h1 = net.get(f'h{args.src}')
    hd = net.get(f'h{args.dst}')
    s1 = net.get('s1')

    # start pcaps
    h1.cmd(f'tcpdump -i h{args.src}-eth0 -s 96 -w /tmp/h{args.src}.pcap & echo $! > /tmp/h{args.src}.tcpdump.pid')
    hd.cmd(f'tcpdump -i h{args.dst}-eth0 -s 96 -w /tmp/h{args.dst}.pcap & echo $! > /tmp/h{args.dst}.tcpdump.pid')
    s1.cmd('tcpdump -i s1-eth1 -s 96 -w /tmp/s1.pcap & echo $! > /tmp/s1.tcpdump.pid')
    time.sleep(2)

    # BEFORE tests
    loss = net.pingAll()
    open(os.path.join(outdir, 'pingall_before.txt'),'w').write(f'loss_percent={loss}\n')
    h1.cmd(f'ping -c 5 10.0.0.{args.dst} | tee /tmp/ping_before.txt')
    shutil.copy('/tmp/ping_before.txt', os.path.join(outdir,'ping_before.txt'))
    hd.cmd('pkill -f iperf3; iperf3 -s -D'); time.sleep(1)
    h1.cmd(f'iperf3 -J -t 8 -c 10.0.0.{args.dst} > /tmp/iperf_before.json')
    shutil.copy('/tmp/iperf_before.json', os.path.join(outdir,'iperf_before.json'))

    # Trigger policy block
    t0 = time.time()
    run_cmd(f"curl -s -X POST '{args.block_url}' -H 'Content-Type: application/json' -d '{{\"src_ip\":\"10.0.0.{args.src}\",\"dst_ip\":\"10.0.0.{args.dst}\"}}' > {outdir}/block_response.json")
    open(os.path.join(outdir,'block_request_epoch_ms.txt'),'w').write(str(int(t0*1000))+'\n')
    time.sleep(3)

    # AFTER tests
    loss2 = net.pingAll()
    open(os.path.join(outdir, 'pingall_after.txt'),'w').write(f'loss_percent={loss2}\n')
    h1.cmd(f'ping -c 5 10.0.0.{args.dst} | tee /tmp/ping_after.txt')
    shutil.copy('/tmp/ping_after.txt', os.path.join(outdir,'ping_after.txt'))
    h1.cmd(f'iperf3 -J -t 8 -c 10.0.0.{args.dst} > /tmp/iperf_after.json')
    shutil.copy('/tmp/iperf_after.json', os.path.join(outdir,'iperf_after.json'))
    hd.cmd('pkill -f iperf3')

    # OVS flows on all switches
    for dpid in range(1, nsw+1):
        net.get(f's{dpid}').cmd(f'ovs-ofctl -O OpenFlow13 dump-flows s{dpid} > /tmp/ovs_flows_s{dpid}.txt')
        shutil.copy(f'/tmp/ovs_flows_s{dpid}.txt', os.path.join(outdir,f'ovs_flows_s{dpid}.txt'))

    # stop pcaps, collect
    try:
        h1.cmd(f'kill -9 $(cat /tmp/h{args.src}.tcpdump.pid)'); hd.cmd(f'kill -9 $(cat /tmp/h{args.dst}.tcpdump.pid)'); s1.cmd('kill -9 $(cat /tmp/s1.tcpdump.pid)')
    except: pass
    for p in [f'/tmp/h{args.src}.pcap', f'/tmp/h{args.dst}.pcap', '/tmp/s1.pcap']:
        if os.path.exists(p): shutil.copy(p, os.path.join(outdir, os.path.basename(p)))
    net.stop()

if __name__ == '__main__':
    main()
