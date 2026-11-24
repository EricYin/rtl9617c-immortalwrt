#!/usr/bin/env python3
"""Host-test multi-queue RX EQ/BID table and L3FE hardware RSS DPID packing.

Slices the region between the BEGIN/END rtl-ni-rss markers of ni_qm.c together
with queue sizing macros from ni.h, compiles it for the host under ASan/UBSan,
and checks:

  * All 4 CPU RX queues use distinct EQs in 0..15, disjoint from DeepQ (1, 2)
    and LAN DeepQ (8)
  * All 4 CPU RX queues use pairwise-disjoint BID windows, disjoint from DeepQ
    (512..1535) and LAN DeepQ (4352..)
  * rtl_ni_rss_dpid_words() packs all 16 hash buckets into the exact Taurus
    L3FE_GLB_CPU_REMAP_DPID0 / DPID1 bit slots with zero stray reserved bits
    across 1..4 queues (plus clamping for 0 and >4)
  * DTSI wires all 4 CPU EPP FIFO interrupts and ni_mib.c exposes ni_rx_q0..3
"""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[4]
TARGET = ROOT / 'target/linux/rtl9617c'
NI = TARGET / 'files-6.18/drivers/net/ethernet/realtek/rtl9617c/ni'
qm_src = (NI / 'ni_qm.c').read_text()
ni_hdr = (NI / 'ni.h').read_text()
mib_src = (NI / 'ni_mib.c').read_text()
dtsi = (TARGET / 'dts/rtk-taurus.dtsi').read_text()

for q in range(4):
    irq_sym = f'TAURUS_IRQ_GLB_QM_CPU_FIFO{q}_REQ'
    if irq_sym not in dtsi:
        sys.exit(f'rtk-taurus.dtsi missing {irq_sym}')
    mib_sym = f'"ni_rx_q{q}"'
    if mib_sym not in mib_src:
        sys.exit(f'ni_mib.c missing {mib_sym}')

region = qm_src[qm_src.index('/* BEGIN rtl-ni-rss'):
                qm_src.index('/* END rtl-ni-rss')]
macros = '\n'.join(re.findall(
    r'^#define (?:RTL_NI_RX_QUEUES|RTL_NI_QM_EQS|RTL_NI_QM_EQ_BUFS|RTL_NI_QM_BUFS)\s+.*$',
    ni_hdr, re.M))

prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
'''

tests = r'''
static u32 unpack_bucket(u32 dpid0, u32 dpid1, unsigned int i)
{
	if (i <= 4)
		return (dpid0 >> (3 * i)) & 0x7U;
	if (i <= 9)
		return (dpid0 >> (3 * i + 1)) & 0x7U;
	if (i <= 14)
		return (dpid1 >> (3 * i - 30)) & 0x7U;
	return (dpid1 >> 16) & 0x7U;
}

static void test_eq_bid_disjoint(void)
{
	bool eq_used[16] = {0};
	unsigned int q, other;

	/* Reserved by DeepQ (1, 2) and LAN DeepQ (8). */
	eq_used[1] = true;
	eq_used[2] = true;
	eq_used[8] = true;

	for (q = 0; q < RTL_NI_RX_QUEUES; q++) {
		u8 eq0 = rtl_ni_rxq_cfgs[q].eq0;
		u8 eq1 = rtl_ni_rxq_cfgs[q].eq1;
		u16 b0 = rtl_ni_rxq_cfgs[q].bid_base;
		u16 b1 = b0 + RTL_NI_QM_BUFS;

		assert(eq0 < 16 && eq1 < 16);
		assert(!eq_used[eq0]);
		eq_used[eq0] = true;
		assert(!eq_used[eq1]);
		eq_used[eq1] = true;

		/* Must not overlap DeepQ [512, 1536) or LAN DeepQ [4352, ...). */
		assert(b1 <= 512 || b0 >= 1536);
		assert(b1 <= 4352);

		for (other = 0; other < q; other++) {
			u16 o0 = rtl_ni_rxq_cfgs[other].bid_base;
			u16 o1 = o0 + RTL_NI_QM_BUFS;

			assert(b1 <= o0 || o1 <= b0);
		}
	}
}

static void test_rss_dpid_words(void)
{
	static const unsigned int cases[] = {0, 1, 2, 3, 4, 8};
	unsigned int c, i;

	for (c = 0; c < sizeof(cases) / sizeof(cases[0]); c++) {
		unsigned int raw = cases[c];
		unsigned int eff = raw < 1 ? 1 : (raw > RTL_NI_RX_QUEUES ?
						  RTL_NI_RX_QUEUES : raw);
		u32 dpid0 = 0xdeadbeefU, dpid1 = 0xdeadbeefU;

		rtl_ni_rss_dpid_words(raw, &dpid0, &dpid1);

		/* Reserved separator/high bits must stay zero. */
		assert((dpid0 & ((1U << 15) | (1U << 31))) == 0);
		assert((dpid1 & ((1U << 15) | (0xfff80000U))) == 0);

		for (i = 0; i < 16; i++)
			assert(unpack_bucket(dpid0, dpid1, i) == (i % eff));
	}
}

int main(void)
{
	test_eq_bid_disjoint();
	test_rss_dpid_words();
	puts("PASS: 4-queue RX EQ/BID disjointness (vs DeepQ 1,2 & LAN-DQ 8); "
	     "L3FE RSS DPID0/DPID1 16-bucket packing & clamping (1..4 queues); "
	     "DTSI 4x FIFO IRQs + ni_rx_q0..3 MIB names");
	return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='pt1500-rx-rss-layout-') as directory:
    test = Path(directory) / 'rss.c'
    binary = Path(directory) / 'rss'
    test.write_text(prefix + macros + '\n' + region + '\n' + tests)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-g',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    str(test), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, env=dict(os.environ))
