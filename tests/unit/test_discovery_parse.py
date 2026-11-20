"""Parsers of OS tool output (R2.1, R4.7). The Windows layouts were taken from a real ``tasklist`` and
``netstat -ano`` run; the Linux and macOS layouts follow the tools' documented output (capturing real
output on those systems is a manual step, see the README)."""
from __future__ import annotations

from chimericmesh.discovery.models import Listener, ProcEntry
from chimericmesh.discovery.parse import (
    parse_lsof,
    parse_netstat_linux,
    parse_netstat_windows,
    parse_ps,
    parse_ss,
    parse_tasklist,
)

SS = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
tcp   LISTEN 0      511          0.0.0.0:80         0.0.0.0:*     users:(("nginx",pid=812,fd=6),("nginx",pid=813,fd=6))
tcp   LISTEN 0      128             [::]:21            [::]:*     users:(("vsftpd",pid=900,fd=3))
udp   UNCONN 0      0                  *:514             *:*
udp   UNCONN 0      0          127.0.0.53%lo:53         0.0.0.0:*     users:(("systemd-resolve",pid=500,fd=13))
tcp   LISTEN 0      4096  [::1]%lo:631  [::]:*
"""


def test_ss_with_and_without_process_info_ipv6_and_udp():
    got = parse_ss(SS)
    assert Listener(80, "tcp", 812, "nginx") in got and Listener(80, "tcp", 813, "nginx") in got
    assert Listener(21, "tcp", 900, "vsftpd") in got
    assert Listener(514, "udp", None, None) in got
    assert Listener(53, "udp", 500, "systemd-resolve") in got
    assert Listener(631, "tcp", None, None) in got
    assert all(x.proto in ("tcp", "udp") for x in got)


NETSTAT_LINUX = """Active Internet connections (only servers)
Proto Recv-Q Send-Q Local Address           Foreign Address         State       PID/Program name
tcp        0      0 0.0.0.0:80              0.0.0.0:*               LISTEN      812/nginx
tcp6       0      0 :::25                   :::*                    LISTEN      -
udp        0      0 0.0.0.0:68              0.0.0.0:*                           455/dhclient
udp        0      0 0.0.0.0:514             0.0.0.0:*                           -
tcp        0      0 0.0.0.0:8080            0.0.0.0:*               LISTEN      77/nginx: master p
"""


def test_netstat_linux():
    got = parse_netstat_linux(NETSTAT_LINUX)
    assert Listener(80, "tcp", 812, "nginx") in got
    assert Listener(25, "tcp", None, None) in got
    assert Listener(68, "udp", 455, "dhclient") in got
    assert Listener(514, "udp", None, None) in got
    assert Listener(8080, "tcp", 77, "nginx") in got
    assert len(got) == 5


TASKLIST = ('"System Idle Process","0","Services","0","8 K"\n'
            '"System","4","Services","0","1,232 K"\n'
            '"nginx.exe","4120","Console","1","12,000 K"\n'
            '"My, Weird \\"App\\".exe","77","Console","1","1 K"\n'
            '"W3WP.EXE","9000","Services","0","1 K"\n'
            'garbage line\n'
            '"onlyone"\n'
            '"bad","notapid","x","y","z"\n')


def test_tasklist_quoted_names_with_commas_and_bad_rows():
    got = parse_tasklist(TASKLIST)
    names = {p.name for p in got}
    assert {"nginx", "w3wp", "system"} <= names
    assert ProcEntry(4120, "nginx") in got
    assert all(p.pid is None or p.pid >= 0 for p in got)
    assert not any(p.name == "bad" for p in got)


NETSTAT_WIN = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:80             0.0.0.0:0              LISTENING       4
  TCP    0.0.0.0:445            0.0.0.0:0              ABHOREN         4
  TCP    [::]:135               [::]:0                 LISTENING       892
  TCP    192.168.1.5:50233      93.184.216.34:443      ESTABLISHED     1234
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       7000
  UDP    0.0.0.0:123            *:*                                    1234
  UDP    [::]:5353              *:*                                    4321
  UDP    127.0.0.1:1900         *:*                                    55
  TCP    0.0.0.0:21
"""


def test_netstat_windows_listening_established_udp_and_localized_state():
    got = parse_netstat_windows(NETSTAT_WIN)
    assert Listener(80, "tcp", 4, None) in got
    assert Listener(445, "tcp", 4, None) in got            # localized state word: foreign ":0" decides
    assert Listener(135, "tcp", 892, None) in got
    assert Listener(8000, "tcp", 7000, None) in got
    assert Listener(123, "udp", 1234, None) in got
    assert Listener(5353, "udp", 4321, None) in got
    assert not any(x.port == 50233 for x in got)           # established: skipped
    assert not any(x.port == 21 for x in got)              # truncated line


PS = """  PID COMM
    1 /sbin/launchd
  812 /usr/sbin/nginx
  900 /Applications/Foo Bar.app/Contents/MacOS/Foo Bar
  901 vsftpd
 bad line
"""


def test_ps_with_spaces_in_path():
    got = parse_ps(PS)
    assert ProcEntry(812, "nginx") in got and ProcEntry(901, "vsftpd") in got
    assert ProcEntry(900, "foo bar") in got and ProcEntry(1, "launchd") in got


LSOF = """COMMAND     PID   USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
nginx       812   root    6u  IPv4 0xabcdef0123456      0t0  TCP *:80 (LISTEN)
nginx       812   root    7u  IPv6 0xabcdef0123457      0t0  TCP [::1]:8080 (LISTEN)
Google\\x20Chrome  300 me   22u  IPv4 0xabcdef012      0t0  UDP 127.0.0.1:5353
mDNSResponder 100 _mdns  8u  IPv4 0xabcd      0t0  UDP 10.0.0.1:5000->10.0.0.2:6000
"""


def test_lsof_decodes_spaces_and_takes_the_local_port():
    got = parse_lsof(LSOF)
    assert Listener(80, "tcp", 812, "nginx") in got
    assert Listener(8080, "tcp", 812, "nginx") in got
    assert Listener(5353, "udp", 300, "google chrome") in got
    assert Listener(5000, "udp", 100, "mdnsresponder") in got


def test_empty_header_only_and_truncated_input():
    for parser in (parse_ss, parse_netstat_linux, parse_tasklist, parse_netstat_windows, parse_ps, parse_lsof):
        assert parser("") == ()
        assert parser("\n\n") == ()
        assert isinstance(parser("tcp LISTEN 0 1 0.0.0.0:8"), tuple)
    assert parse_ss(SS.splitlines()[0]) == ()
    assert isinstance(parse_ss(SS[:-37]), tuple)


def test_results_are_capped_at_20000():
    text = "\n".join(f"tcp LISTEN 0 1 0.0.0.0:{1 + i % 60000} 0.0.0.0:*" for i in range(30000))
    assert len(parse_ss(text)) == 20000


def test_ports_out_of_range_are_skipped():
    assert parse_ss("tcp LISTEN 0 1 0.0.0.0:0 0.0.0.0:*\ntcp LISTEN 0 1 0.0.0.0:65536 0.0.0.0:*") == ()


def test_hostile_names_are_normalized():
    got = parse_ss('tcp LISTEN 0 1 0.0.0.0:80 0.0.0.0:* users:(("ng\x1bi\x00nx.exe",pid=1,fd=1))')
    assert got == (Listener(80, "tcp", 1, "nginx"),)
