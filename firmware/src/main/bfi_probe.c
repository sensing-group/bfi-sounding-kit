/*
 * bfi-probe : ESP32-C5 passive 802.11 probe for beamforming sounding frames.
 *
 * Purpose is the week-1 feasibility gate, not a product. It answers one question:
 * on a given channel, what does a 20 MHz-only C5 actually receive?
 *
 * Specifically we care about two frames:
 *   NDPA   - control, type 1 subtype 5. The router's *request*. Should be sent
 *            non-HT duplicate at a legacy rate, hence readable at 20 MHz even on
 *            an 80/160 MHz link. THIS IS THE UNVERIFIED CLAIM.
 *   BF rpt - management, type 0 subtype 14 (Action No Ack), category 21 (VHT) /
 *            30 (HE) / 36 (EHT). The device's *answer*. Known to be sent at the
 *            full link width, so expected to be invisible on a wide link.
 *
 * Deliberately does no on-device bit parsing of STA Info or Givens angles: it
 * hex-dumps and we decode on the host. A wrong offset here would produce a
 * confidently wrong feasibility answer.
 *
 * Two further counters, added for the class campaign:
 *   fail  - frames the radio detected but could not decode, bucketed by the PHY
 *           format in rx_ctrl.cur_bb_format. Measured at ICV-HSG on 2026-09-23:
 *           17 failures while a co-located 80 MHz reference saw about 50,000
 *           VHT frames pass. The C5 does NOT surface wide transmissions as
 *           failed receptions, so this counter cannot tell "nothing happened"
 *           from "I could not hear it". Kept because it costs nothing and
 *           records the negative.
 *   data  - per-device activity counters. Data frames are counted when they are
 *           readable, but on a wide network almost none are (53 of 66,000 in
 *           the same test). The signal that works is the acknowledgements:
 *           ACK and BlockAck are always sent in a legacy frame, and the C5
 *           heard 95 to 98 percent of them, with the device address attached.
 *           Counters only: no payload and no header is ever stored.
 */

#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_console.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "argtable3/argtable3.h"
#include "sdkconfig.h"

/* A 4x1 80 MHz VHT report is 912 B, but a 2-stream device behind a 4-antenna
 * router sends about 1,500 B (VHT) or 1,600 B (HE) at 80 MHz. At 1024 those
 * were cut short and could not be decoded. */
#define DUMP_BYTES 2048
#define QUEUE_LEN  24     /* bigger records, fewer of them: ~50 kB */

static const char *TAG = "probe";

typedef struct {
    int64_t  us;
    int      rssi;
    uint16_t sig_len;
    uint8_t  channel;
    uint8_t  type;
    uint8_t  subtype;
    uint16_t ndump;                /* NOT uint8_t: 327 would wrap to 71 */
    uint8_t  rxctrl[16];        /* raw head of rx_ctrl, decoded host-side */
    uint8_t  buf[DUMP_BYTES];
} rec_t;

static QueueHandle_t q;

/* counters ------------------------------------------------------------- */

static volatile uint32_t c_total, c_dropped;
static volatile uint32_t c_type[4];
static volatile uint32_t c_mgmt[16], c_ctrl[16];
static volatile uint32_t c_ndpa, c_bfrpt, c_beacon, c_trigger;
static volatile uint32_t c_other;           /* soundings of other networks, not dumped */
static volatile uint32_t c_cat[64];         /* action category histogram */
static volatile int      last_rssi_ndpa = 0, last_rssi_bf = 0;

/* reception format histograms, indexed by rx_ctrl.cur_bb_format:
 * 0=11b 1=11a/g 2=HT 3=VHT 4=HE_SU 5=HE_MU 6=HE_ERSU 7=HE_TB 11=VHT_MU */
static volatile uint32_t c_bb_ok[16], c_bb_fail[16];
static volatile uint32_t c_fail, c_misc;

static bool dump_enabled = false;   /* off until asked: see crash notes */
static volatile uint32_t beacon_left = 0;   /* beacons still to dump, see cmd_beacon */
static bool fail_enabled = false;   /* count undecodable frames */
static bool data_enabled = false;   /* count data frames per device */

/* per-device data activity. Addresses live here only to group the counters;
 * nothing is queued, dumped or stored on the board. */
#define ACT_SLOTS 16
#define ACT_IDLE_DROP 30            /* free a slot after this many idle dumps */

typedef struct {
    uint8_t  mac[6];
    uint8_t  used;
    uint8_t  idle;
    uint8_t  bb;
    int8_t   rssi;
    uint32_t up_frames, up_bytes, dn_frames, dn_bytes;
    uint32_t ack;                   /* ACKs addressed to it: it transmitted */
    uint32_t ba_tx, ba_rx;          /* block acks it sent / it received */
} act_t;

static act_t act[ACT_SLOTS];
static volatile uint32_t c_act_full;        /* data frames that found no slot */
static uint8_t bssid_filter[6];
static bool    bssid_set = false;

/* Find the slot of one device, optionally creating it. Group addresses get no
 * slot: they are the access point talking to everyone, not a device.
 * Runs in the Wi-Fi task: a 6-byte compare over at most 16 slots. */
static act_t * IRAM_ATTR slot(const uint8_t *mac, bool create)
{
    if (mac[0] & 0x01) return NULL;
    int free_slot = -1;
    for (int i = 0; i < ACT_SLOTS; i++) {
        if (!act[i].used) { if (free_slot < 0) free_slot = i; continue; }
        if (memcmp(act[i].mac, mac, 6) == 0) { act[i].idle = 0; return &act[i]; }
    }
    if (!create) return NULL;
    if (free_slot < 0) { c_act_full++; return NULL; }
    memset(&act[free_slot], 0, sizeof(act_t));
    memcpy(act[free_slot].mac, mac, 6);
    act[free_slot].used = 1;
    return &act[free_slot];
}

/* Data frames carry the BSSID, so they can be attributed to one network.
 * On a wide network almost none of them are readable at 20 MHz, which is why
 * the control frames below matter more than this. */
static void IRAM_ATTR count_data(const uint8_t *f, uint16_t len, int rssi, uint8_t bb)
{
    const uint8_t *dev, *bss;
    bool up;

    switch (f[1] & 0x03) {                  /* ToDS | FromDS */
    case 0x01: bss = f + 4;  dev = f + 10; up = true;  break;   /* to AP  : a1 BSSID, a2 source */
    case 0x02: bss = f + 10; dev = f + 4;  up = false; break;   /* from AP: a1 dest,  a2 BSSID  */
    default: return;                        /* ad-hoc or WDS: no single device */
    }
    if (bssid_set && memcmp(bss, bssid_filter, 6) != 0) return;

    act_t *s = slot(dev, true);
    if (!s) return;
    s->rssi = (int8_t)rssi;
    s->bb   = bb;
    if (up) { s->up_frames++; s->up_bytes += len; }
    else    { s->dn_frames++; s->dn_bytes += len; }
}

/* Acknowledgements are always sent in a legacy frame, so a 20 MHz listener
 * hears them even when the data they acknowledge is invisible to it. They are
 * the activity signal on a wide network.
 *   ACK        (subtype 13): RA only. An ACK to a device means that device
 *                            transmitted something.
 *   BlockAck   (subtype  9): RA and TA, so both directions can be attributed.
 * Needs the BSSID, otherwise the neighbours' traffic lands in the same slots. */
static void IRAM_ATTR count_ctrl(const uint8_t *f, uint16_t len, uint8_t subtype, int rssi)
{
    if (!bssid_set) return;

    if (subtype == 13 && len >= 10) {               /* ACK */
        act_t *s = slot(f + 4, false);              /* only devices already known */
        if (s) { s->ack++; s->rssi = (int8_t)rssi; }
        return;
    }
    if ((subtype == 9 || subtype == 8) && len >= 16) {   /* BlockAck, BlockAckReq */
        const uint8_t *ra = f + 4, *ta = f + 10;
        act_t *s;
        if (memcmp(ra, bssid_filter, 6) == 0)      { s = slot(ta, true); if (s) s->ba_tx++; }
        else if (memcmp(ta, bssid_filter, 6) == 0) { s = slot(ra, true); if (s) s->ba_rx++; }
        else return;
        if (s) s->rssi = (int8_t)rssi;
    }
}

/* With a BSSID set, only the soundings of that network are dumped: requests
 * and triggers the AP sent (its address may carry the bandwidth-signalling bit),
 * reports addressed to it, its own beacons. The neighbours' frames are counted
 * in c_other and otherwise ignored. */
static bool IRAM_ATTR own_bss(uint8_t type, const uint8_t *f, uint16_t len)
{
    if (!bssid_set) return true;
    if (type == 1) {
        if (len < 16) return false;
        return (f[10] & 0xFE) == bssid_filter[0] &&
               memcmp(f + 11, bssid_filter + 1, 5) == 0;
    }
    if (len < 22) return false;
    return memcmp(f + 4, bssid_filter, 6) == 0 || memcmp(f + 16, bssid_filter, 6) == 0;
}

static void IRAM_ATTR cb(void *recv, wifi_promiscuous_pkt_type_t ptype)
{
    const wifi_promiscuous_pkt_t *p = (wifi_promiscuous_pkt_t *)recv;
    const uint8_t *f = p->payload;
    uint16_t len = p->rx_ctrl.sig_len;
    uint8_t  bb  = p->rx_ctrl.cur_bb_format & 0x0F;

    c_total++;

    /* Undecodable reception: the body is meaningless, the PHY header is not.
     * Bucket it by format and stop here, so a corrupt frame can never be
     * mistaken for an NDPA or a report. */
    if (ptype == WIFI_PKT_MISC || p->rx_ctrl.rx_state != 0) {
        c_fail++;
        c_bb_fail[bb]++;
        if (ptype == WIFI_PKT_MISC) c_misc++;
        return;
    }
    c_bb_ok[bb]++;

    if (len < 2) return;

    uint8_t fc0     = f[0];
    uint8_t type    = (fc0 >> 2) & 0x03;
    uint8_t subtype = (fc0 >> 4) & 0x0F;

    c_type[type]++;
    if (type == 0) c_mgmt[subtype]++;
    else if (type == 1) {
        c_ctrl[subtype]++;
        if (data_enabled) count_ctrl(f, len, subtype, p->rx_ctrl.rssi);
    }
    else if (type == 2) {
        if (data_enabled && len >= 24) count_data(f, len, p->rx_ctrl.rssi, bb);
        return;                              /* data is counted, never dumped */
    }

    bool interesting = false;

    if (type == 1 && subtype == 5) {            /* NDPA */
        if (!own_bss(type, f, len)) { c_other++; return; }
        c_ndpa++; last_rssi_ndpa = p->rx_ctrl.rssi; interesting = true;
        /* the sounded device is named here, so it gets a slot even if all its
         * own traffic is invisible at 20 MHz */
        if (data_enabled && len >= 16) slot(f + 4, true);
    } else if (type == 1 && subtype == 2) {     /* Trigger (incl. BFRP) */
        if (!own_bss(type, f, len)) { c_other++; return; }
        c_trigger++; interesting = true;
    } else if (type == 0 && subtype == 14) {    /* Action No Ack */
        if (len >= 25) {
            uint8_t cat = f[24];
            c_cat[cat & 0x3F]++;
            if (cat == 21 || cat == 30 || cat == 36) {
                if (!own_bss(type, f, len)) { c_other++; return; }
                c_bfrpt++; last_rssi_bf = p->rx_ctrl.rssi; interesting = true;
                if (data_enabled) slot(f + 10, true);   /* the replying device */
            }
        }
    } else if (type == 0 && subtype == 8) {
        if (!own_bss(type, f, len)) return;
        c_beacon++;
        /* A few beacons on request: they carry the router's capabilities
         * (HT/VHT/HE, beamformer bits, sounding dimensions) and often its
         * make and model in the WPS element. Parsed on the host. */
        if (beacon_left && (!bssid_set || (len >= 22 && memcmp(f + 16, bssid_filter, 6) == 0))) {
            beacon_left--;
            interesting = true;
        }
    }

    if (!interesting || !dump_enabled) return;

    /* static: a 2 kB record does not belong on the Wi-Fi task's stack, and this
     * callback only ever runs in that one task */
    static rec_t r;
    r.us      = esp_timer_get_time();
    r.rssi    = p->rx_ctrl.rssi;
    r.sig_len = len;
    r.channel = p->rx_ctrl.channel;
    r.type    = type;
    r.subtype = subtype;
    r.ndump   = len < DUMP_BYTES ? len : DUMP_BYTES;
    memcpy(r.rxctrl, &p->rx_ctrl, sizeof(r.rxctrl));
    memcpy(r.buf, f, r.ndump);

    if (xQueueSend(q, &r, 0) != pdTRUE) c_dropped++;
}

/* One line per active device per second. Read and reset from the printer task
 * while the Wi-Fi task keeps incrementing: a race here can only lose a frame
 * from a count, which is acceptable for an activity signal. */
static void dump_activity(int64_t now)
{
    for (int i = 0; i < ACT_SLOTS; i++) {
        if (!act[i].used) continue;
        if (!act[i].up_frames && !act[i].dn_frames &&
            !act[i].ack && !act[i].ba_tx && !act[i].ba_rx) {
            if (++act[i].idle > ACT_IDLE_DROP) memset(&act[i], 0, sizeof(act[i]));
            continue;
        }
        printf("A %lld %02x:%02x:%02x:%02x:%02x:%02x %" PRIu32 " %" PRIu32
               " %" PRIu32 " %" PRIu32 " %" PRIu32 " %" PRIu32 " %" PRIu32
               " %d %u\n", now,
               act[i].mac[0], act[i].mac[1], act[i].mac[2],
               act[i].mac[3], act[i].mac[4], act[i].mac[5],
               act[i].up_frames, act[i].up_bytes, act[i].dn_frames, act[i].dn_bytes,
               act[i].ack, act[i].ba_tx, act[i].ba_rx,
               act[i].rssi, act[i].bb);
        act[i].up_frames = act[i].up_bytes = act[i].dn_frames = act[i].dn_bytes = 0;
        act[i].ack = act[i].ba_tx = act[i].ba_rx = 0;
    }
    fflush(stdout);
}

/* Reception formats seen in the last second: what the radio decoded and what it
 * only detected. Empty seconds print nothing. */
static uint32_t prev_ok[16], prev_fail[16];   /* last printed format counts */

static void dump_formats(int64_t now)
{
    char line[192];
    int n = snprintf(line, sizeof line, "Y %lld ok", now);
    bool any = false;
    for (int i = 0; i < 16; i++) {
        uint32_t v = c_bb_ok[i];
        if (v == prev_ok[i]) continue;
        n += snprintf(line + n, sizeof line - n, " %d=%" PRIu32, i, v - prev_ok[i]);
        prev_ok[i] = v; any = true;
    }
    n += snprintf(line + n, sizeof line - n, " fail");
    for (int i = 0; i < 16; i++) {
        uint32_t v = c_bb_fail[i];
        if (v == prev_fail[i]) continue;
        n += snprintf(line + n, sizeof line - n, " %d=%" PRIu32, i, v - prev_fail[i]);
        prev_fail[i] = v; any = true;
    }
    if (!any) return;
    printf("%s\n", line);
    fflush(stdout);
}

static void printer(void *arg)
{
    static rec_t r;                 /* 2 kB: kept off the task stack */
    int64_t next = esp_timer_get_time() + 1000000;
    for (;;) {
        int64_t now = esp_timer_get_time();
        if (now >= next) {
            next = now + 1000000;
            if (data_enabled) dump_activity(now);
            if (data_enabled || fail_enabled) dump_formats(now);
        }
        if (xQueueReceive(q, &r, pdMS_TO_TICKS(20)) == pdTRUE) {
            /* The console USB-Serial/JTAG path silently truncates a single
             * large write (observed: a 327 B report arrived as 71 B). Emit the
             * record in small chunks and flush each one. */
            char hdr[64];
            int hn = snprintf(hdr, sizeof hdr, "F %lld %u %u %d %u %u ",
                              r.us, r.type, r.subtype, r.rssi, r.sig_len, r.channel);
            fwrite(hdr, 1, hn, stdout); fflush(stdout);

            char hx[65];
            const uint8_t *parts[2] = { r.rxctrl, r.buf };
            int lens[2] = { 16, r.ndump };
            for (int q = 0; q < 2; q++) {
                for (int i = 0; i < lens[q]; ) {
                    int n = 0;
                    while (n < 32 && i < lens[q]) {
                        snprintf(hx + 2 * n, 3, "%02x", parts[q][i]);
                        n++; i++;
                    }
                    fwrite(hx, 1, 2 * n, stdout); fflush(stdout);
                }
                if (q == 0) { fputc(' ', stdout); fflush(stdout); }
            }
            fputc('\n', stdout); fflush(stdout);
        }
    }
}

/* commands ------------------------------------------------------------- */

static void clear_counters(void)
{
    c_total = c_dropped = c_ndpa = c_bfrpt = c_beacon = c_trigger = c_other = 0;
    c_fail = c_misc = c_act_full = 0;
    memset((void *)c_type, 0, sizeof(c_type));
    memset((void *)c_mgmt, 0, sizeof(c_mgmt));
    memset((void *)c_ctrl, 0, sizeof(c_ctrl));
    memset((void *)c_cat,  0, sizeof(c_cat));
    memset((void *)c_bb_ok,   0, sizeof(c_bb_ok));
    memset((void *)c_bb_fail, 0, sizeof(c_bb_fail));
    memset(prev_ok,   0, sizeof(prev_ok));
    memset(prev_fail, 0, sizeof(prev_fail));
    memset(act, 0, sizeof(act));
}

/* The mask is composed from the flags, so `data` and `fail` can be switched
 * during a capture without disturbing the rest. */
static void apply_filter(void)
{
    uint32_t m = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_CTRL;
    if (data_enabled) m |= WIFI_PROMIS_FILTER_MASK_DATA |
                           WIFI_PROMIS_FILTER_MASK_DATA_MPDU |
                           WIFI_PROMIS_FILTER_MASK_DATA_AMPDU;
    if (fail_enabled) m |= WIFI_PROMIS_FILTER_MASK_MISC |
                           WIFI_PROMIS_FILTER_MASK_FCSFAIL;
    wifi_promiscuous_filter_t filt = { .filter_mask = m };
    esp_err_t e = esp_wifi_set_promiscuous_filter(&filt);
    printf("R filter=0x%08" PRIx32 " data=%s fail=%s %s\n", m,
           data_enabled ? "on" : "off", fail_enabled ? "on" : "off",
           esp_err_to_name(e));
}

static int cmd_stats(int argc, char **argv)
{
    uint8_t pri = 0; wifi_second_chan_t sec = 0;
    esp_wifi_get_channel(&pri, &sec);
    printf("S ch=%u sec=%d total=%" PRIu32 " dropped=%" PRIu32
           " mgmt=%" PRIu32 " ctrl=%" PRIu32 " data=%" PRIu32
           " | NDPA=%" PRIu32 " (rssi %d) BFRPT=%" PRIu32 " (rssi %d)"
           " TRIG=%" PRIu32 " BEACON=%" PRIu32 " OTHER=%" PRIu32 "\n",
           pri, sec, c_total, c_dropped, c_type[0], c_type[1], c_type[2],
           c_ndpa, last_rssi_ndpa, c_bfrpt, last_rssi_bf, c_trigger, c_beacon, c_other);
    printf("S mgmt_subtypes:");
    for (int i = 0; i < 16; i++) if (c_mgmt[i]) printf(" %d=%" PRIu32, i, c_mgmt[i]);
    printf("\nS ctrl_subtypes:");
    for (int i = 0; i < 16; i++) if (c_ctrl[i]) printf(" %d=%" PRIu32, i, c_ctrl[i]);
    printf("\nS action_categories:");
    for (int i = 0; i < 64; i++) if (c_cat[i]) printf(" %d=%" PRIu32, i, c_cat[i]);
    printf("\nS bb_ok:");
    for (int i = 0; i < 16; i++) if (c_bb_ok[i]) printf(" %d=%" PRIu32, i, c_bb_ok[i]);
    printf("\nS bb_fail:");
    for (int i = 0; i < 16; i++) if (c_bb_fail[i]) printf(" %d=%" PRIu32, i, c_bb_fail[i]);
    printf("\nS fail=%" PRIu32 " misc=%" PRIu32 " act_full=%" PRIu32 "\n",
           c_fail, c_misc, c_act_full);
    return 0;
}

static struct { struct arg_int *n; struct arg_end *end; } ch_args;

static int cmd_ch(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **)&ch_args) != 0) { arg_print_errors(stderr, ch_args.end, argv[0]); return 1; }
    int n = ch_args.n->ival[0];
    wifi_band_mode_t bm = (n >= 32) ? WIFI_BAND_MODE_5G_ONLY : WIFI_BAND_MODE_2G_ONLY;
    esp_err_t e1 = esp_wifi_set_band_mode(bm);
    esp_err_t e2 = esp_wifi_set_channel((uint8_t)n, WIFI_SECOND_CHAN_NONE);
    printf("R set_band_mode=%s set_channel=%s ch=%d\n",
           esp_err_to_name(e1), esp_err_to_name(e2), n);
    clear_counters();
    return 0;
}

static struct { struct arg_str *mode; struct arg_end *end; } dump_args;

static int cmd_dump(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **)&dump_args) != 0) { arg_print_errors(stderr, dump_args.end, argv[0]); return 1; }
    dump_enabled = (strcmp(dump_args.mode->sval[0], "on") == 0);
    printf("R dump=%s\n", dump_enabled ? "on" : "off");
    return 0;
}

static int cmd_clear(int argc, char **argv) { clear_counters(); printf("R cleared\n"); return 0; }

static struct { struct arg_str *mode; struct arg_end *end; } onoff_args;

static int cmd_data(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **)&onoff_args) != 0) { arg_print_errors(stderr, onoff_args.end, argv[0]); return 1; }
    data_enabled = (strcmp(onoff_args.mode->sval[0], "on") == 0);
    if (!data_enabled) memset(act, 0, sizeof(act));
    apply_filter();
    return 0;
}

static int cmd_fail(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **)&onoff_args) != 0) { arg_print_errors(stderr, onoff_args.end, argv[0]); return 1; }
    fail_enabled = (strcmp(onoff_args.mode->sval[0], "on") == 0);
    apply_filter();
    return 0;
}

static int cmd_beacon(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **)&ch_args) != 0) { arg_print_errors(stderr, ch_args.end, argv[0]); return 1; }
    beacon_left = (uint32_t)ch_args.n->ival[0];
    printf("R beacon=%" PRIu32 " (needs dump on)\n", beacon_left);
    return 0;
}

static struct { struct arg_str *mac; struct arg_end *end; } bssid_args;

/* Restrict the recording and the data counters to one access point, so a
 * student records neither the neighbours' soundings nor their traffic. "any"
 * removes the restriction. */
static int cmd_bssid(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **)&bssid_args) != 0) { arg_print_errors(stderr, bssid_args.end, argv[0]); return 1; }
    const char *s = bssid_args.mac->sval[0];
    if (strcmp(s, "any") == 0) {
        bssid_set = false;
        printf("R bssid=any\n");
    } else {
        uint8_t m[6];
        if (sscanf(s, "%2hhx:%2hhx:%2hhx:%2hhx:%2hhx:%2hhx",
                   &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) != 6) {
            printf("R bssid=error\n");
            return 1;
        }
        memcpy(bssid_filter, m, 6);
        bssid_set = true;
        memset(act, 0, sizeof(act));
        slot(m, true);          /* ACKs to the AP are its downlink transmissions */
        printf("R bssid=%02x:%02x:%02x:%02x:%02x:%02x\n", m[0], m[1], m[2], m[3], m[4], m[5]);
    }
    memset(act, 0, sizeof(act));
    return 0;
}

/* HT40 attempt: the one legal way a C5 exceeds 20 MHz, and only in 11n mode. */
static int cmd_ht40(int argc, char **argv)
{
    wifi_bandwidths_t bw = { .ghz_2g = WIFI_BW20, .ghz_5g = WIFI_BW40 };
    esp_err_t e = esp_wifi_set_bandwidths(WIFI_IF_STA, &bw);
    printf("R set_bandwidths(5g=HT40)=%s\n", esp_err_to_name(e));
    return 0;
}

/* ch40: listen 40 MHz wide (HT40, Wi-Fi 4 only) with the secondary channel above
 * the primary. Test for networks that send their requests in a 40 MHz frame. */
static int cmd_ch40(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **)&ch_args) != 0) { arg_print_errors(stderr, ch_args.end, argv[0]); return 1; }
    int n = ch_args.n->ival[0];
    esp_wifi_set_band_mode(n >= 32 ? WIFI_BAND_MODE_5G_ONLY : WIFI_BAND_MODE_2G_ONLY);
    wifi_bandwidths_t bw = { .ghz_2g = WIFI_BW40, .ghz_5g = WIFI_BW40 };
    esp_err_t e1 = esp_wifi_set_bandwidths(WIFI_IF_STA, &bw);
    esp_err_t e2 = esp_wifi_set_channel((uint8_t)n, WIFI_SECOND_CHAN_ABOVE);
    printf("R set_bandwidths=%s set_channel(above)=%s ch=%d\n",
           esp_err_to_name(e1), esp_err_to_name(e2), n);
    clear_counters();
    return 0;
}

/* scan: list the networks the C5 can hear, by listening only.
 * Passive scan = wait for beacons on each channel; the C5 never transmits.
 * One line per network, SSID last because it may contain spaces:
 *   W <bssid> <primary> <bw: 1=20 2=40 3=80 4=160 5=80+80> <centre ch> <rssi> <auth> <std> <ssid>
 *   W end <count>                                                                 */
static int cmd_scan(int argc, char **argv)
{
    uint8_t pri = 0; wifi_second_chan_t sec = 0;
    esp_wifi_get_channel(&pri, &sec);
    esp_wifi_set_promiscuous(false);
    esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO);

    wifi_scan_config_t sc = { .show_hidden = true, .scan_type = WIFI_SCAN_TYPE_PASSIVE };
    sc.scan_time.passive = 150;                       /* > one beacon interval (102 ms) */
    esp_err_t e = esp_wifi_scan_start(&sc, true);

    uint16_t n = 0;
    esp_wifi_scan_get_ap_num(&n);
    if (n > 64) n = 64;
    wifi_ap_record_t *ap = calloc(n ? n : 1, sizeof(*ap));
    if (e == ESP_OK && ap) esp_wifi_scan_get_ap_records(&n, ap);
    for (int i = 0; i < n && ap; i++) {
        const char *std = ap[i].phy_11ax ? "ax" : ap[i].phy_11ac ? "ac" :
                          ap[i].phy_11n ? "n" : ap[i].phy_11a ? "a" : "g";
        printf("W %02x:%02x:%02x:%02x:%02x:%02x %u %d %u %d %d %s %s\n",
               ap[i].bssid[0], ap[i].bssid[1], ap[i].bssid[2],
               ap[i].bssid[3], ap[i].bssid[4], ap[i].bssid[5],
               ap[i].primary, (int)ap[i].bandwidth, ap[i].vht_ch_freq1,
               ap[i].rssi, (int)ap[i].authmode, std, (char *)ap[i].ssid);
    }
    printf("W end %u %s\n", n, esp_err_to_name(e));
    free(ap);

    /* back to where we were, listening */
    if (pri) {
        esp_wifi_set_band_mode(pri >= 32 ? WIFI_BAND_MODE_5G_ONLY : WIFI_BAND_MODE_2G_ONLY);
        esp_wifi_set_channel(pri, WIFI_SECOND_CHAN_NONE);
    }
    esp_wifi_set_promiscuous(true);
    return 0;
}

void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));   /* needed for scan; never connects */

    /* Switzerland, manual: needed to unlock 5 GHz beyond the world domain. */
    wifi_country_t country = { .cc = "CH", .schan = 1, .nchan = 13,
                               .policy = WIFI_COUNTRY_POLICY_MANUAL };
    esp_err_t ec = esp_wifi_set_country(&country);
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "set_country=%s", esp_err_to_name(ec));

    q = xQueueCreate(QUEUE_LEN, sizeof(rec_t));
    xTaskCreate(printer, "printer", 4096, NULL, 4, NULL);

    apply_filter();

    wifi_promiscuous_filter_t cfilt = { .filter_mask = 0xFFFFFFFF };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_ctrl_filter(&cfilt));

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(cb));
    /* Enabling the sniffer immediately after esp_wifi_start() intermittently
     * hits an interrupt-watchdog panic inside ic_enable_sniffer (seen on a
     * reboot after the host power-cycled the USB port), and the board then
     * crash-loops at this line. Give the driver time to settle first. */
    vTaskDelay(pdMS_TO_TICKS(300));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t rc = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    rc.prompt = "probe>";
#if defined(CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG)
    esp_console_dev_usb_serial_jtag_config_t uc = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&uc, &rc, &repl));
#else
    esp_console_dev_uart_config_t uc = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_uart(&uc, &rc, &repl));
#endif

    ch_args.n = arg_int1(NULL, NULL, "<n>", "channel number");
    ch_args.end = arg_end(2);
    const esp_console_cmd_t c1 = { .command = "ch", .help = "set channel (picks band automatically)",
                                   .func = &cmd_ch, .argtable = &ch_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c1));

    dump_args.mode = arg_str1(NULL, NULL, "<on|off>", "per-frame hex dump");
    dump_args.end = arg_end(2);
    const esp_console_cmd_t c2 = { .command = "dump", .help = "toggle per-frame dump",
                                   .func = &cmd_dump, .argtable = &dump_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c2));

    const esp_console_cmd_t c3 = { .command = "stats", .help = "print counters", .func = &cmd_stats };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c3));
    const esp_console_cmd_t c4 = { .command = "clear", .help = "reset counters", .func = &cmd_clear };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c4));
    const esp_console_cmd_t c5 = { .command = "ht40", .help = "try HT40 on 5 GHz (11n only)", .func = &cmd_ht40 };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c5));

    const esp_console_cmd_t c6 = { .command = "scan", .help = "list networks (listen-only scan)", .func = &cmd_scan };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c6));

    const esp_console_cmd_t c7 = { .command = "ch40", .help = "listen 40 MHz wide (HT40, secondary above)",
                                   .func = &cmd_ch40, .argtable = &ch_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c7));

    onoff_args.mode = arg_str1(NULL, NULL, "<on|off>", "on or off");
    onoff_args.end = arg_end(2);
    const esp_console_cmd_t c8 = { .command = "data", .help = "count data frames per device",
                                   .func = &cmd_data, .argtable = &onoff_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c8));
    const esp_console_cmd_t c9 = { .command = "fail", .help = "count frames the radio cannot decode",
                                   .func = &cmd_fail, .argtable = &onoff_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c9));

    bssid_args.mac = arg_str1(NULL, NULL, "<mac|any>", "record only this AP's network");
    bssid_args.end = arg_end(2);
    const esp_console_cmd_t c10 = { .command = "bssid", .help = "record only one AP's network",
                                    .func = &cmd_bssid, .argtable = &bssid_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c10));

    const esp_console_cmd_t c11 = { .command = "beacon", .help = "dump the next <n> beacons of the chosen AP",
                                    .func = &cmd_beacon, .argtable = &ch_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&c11));

    printf("\nbfi-probe ready. commands: scan | ch <n> | ch40 <n> | stats | clear | dump on|off"
           " | data on|off | fail on|off | bssid <mac|any> | beacon <n> | ht40\n");
    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}
