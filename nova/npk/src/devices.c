/*
 * NPK Milestone 7 device abstraction and timer processing.
 *
 * Nova ISA v1.0 has no MMIO, device tree, interrupt controller, framebuffer,
 * keyboard, disk, or network. This file therefore does not invent one. It owns
 * two capabilities the platform genuinely defines:
 *
 *   - a console sink reached through the existing SYS_WRITE trap path; the
 *     bytes remain in the Milestone 6 write log, which the host correlates
 *     with the reference model's machine.io_log evidence;
 *   - the architectural one-shot timer and interrupt CSRs time, timecmp, ie,
 *     ip, and ivec. Interrupts arrive through the real Milestone 3 vector;
 *     this file classifies the source, records bounded evidence, and
 *     reprograms timecmp once per timer interrupt.
 *
 * Unsupported block, input, and network kinds are explicit rejection entries
 * in the capability table, not dormant drivers.
 */

#include "npk_core.h"

#define NPK_DEVICE_STATE_ADDRESS 0xA1000
#define NPK_DEVICE_STATE_MAGIC 0x4E504B44
#define NPK_DEVICE_STATE_VERSION 1

#define NPK_DEVICE_INTERRUPT_LOG_ADDRESS 0xA1090
#define NPK_DEVICE_INTERRUPT_ENTRY_SIZE 32
#define NPK_DEVICE_INTERRUPT_SLOTS 16

#define NPK_DEVICE_CAPABILITY_TABLE_ADDRESS 0xA1290
#define NPK_DEVICE_CAPABILITY_ENTRY_SIZE 32
#define NPK_DEVICE_CAPABILITY_SLOTS 8

#define NPK_DEVICE_KIND_CONSOLE 1
#define NPK_DEVICE_KIND_TIMER 2
#define NPK_DEVICE_KIND_BLOCK 3
#define NPK_DEVICE_KIND_INPUT 4
#define NPK_DEVICE_KIND_NETWORK 5

#define NPK_DEVICE_STATUS_SUPPORTED 1
#define NPK_DEVICE_STATUS_UNSUPPORTED 2

#define NPK_CLASS_SOFTWARE_INTERRUPT 8
#define NPK_CLASS_TIMER_INTERRUPT 9
#define NPK_CLASS_EXTERNAL_INTERRUPT 10

#define NPK_SOURCE_SOFTWARE 0
#define NPK_SOURCE_TIMER 1
#define NPK_SOURCE_EXTERNAL 2

/* The unoptimized C trap path is longer than a short interval; leave enough
 * time for one handler to reprogram the compare before another can fire. */
#define NPK_DEVICE_TIMER_INTERVAL 16384
#define NPK_U64_MAX 0xFFFFFFFFFFFFFFFF

struct NpkDeviceState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 init_status;
    npk_u64 interrupt_count;
    npk_u64 console_calls;
    npk_u64 console_bytes;
    npk_u64 timer_interrupts;
    npk_u64 timer_reprograms;
    npk_u64 software_interrupts;
    npk_u64 external_interrupts;
    npk_u64 last_source;
    npk_u64 last_ecause;
    npk_u64 last_time;
    npk_u64 last_timecmp;
    npk_u64 last_ie;
    npk_u64 last_ip;
    npk_u64 last_ivec;
    npk_u64 timer_interval;
};

struct NpkDeviceCapability {
    npk_u64 kind;
    npk_u64 status;
    npk_u64 reserved[2];
};

struct NpkDeviceInterrupt {
    npk_u64 source;
    npk_u64 time;
    npk_u64 timecmp;
    npk_u64 ie;
    npk_u64 ip;
    npk_u64 ivec;
    npk_u64 ecause;
    npk_u64 epc;
};

extern npk_u64 npk_device_read_time(void);
extern npk_u64 npk_device_read_timecmp(void);
extern void npk_device_write_timecmp(npk_u64 value);
extern npk_u64 npk_device_read_ie(void);
extern void npk_device_write_ie(npk_u64 value);
extern npk_u64 npk_device_read_ip(void);
extern void npk_device_write_ip(npk_u64 value);
extern npk_u64 npk_device_read_ivec(void);
extern void npk_device_write_ivec(npk_u64 value);
extern npk_u64 npk_device_read_vbar(void);

static struct NpkDeviceState *npk_device_state(void)
{
    return (struct NpkDeviceState *)NPK_DEVICE_STATE_ADDRESS;
}

static struct NpkDeviceCapability *npk_capabilities(void)
{
    return (struct NpkDeviceCapability *)NPK_DEVICE_CAPABILITY_TABLE_ADDRESS;
}

static struct NpkDeviceInterrupt *npk_interrupt_record(npk_u64 index)
{
    npk_u64 base = NPK_DEVICE_INTERRUPT_LOG_ADDRESS +
                   (index & (NPK_DEVICE_INTERRUPT_SLOTS - 1)) *
                   NPK_DEVICE_INTERRUPT_ENTRY_SIZE;
    return (struct NpkDeviceInterrupt *)base;
}

static npk_u64 npk_load_byte(npk_u64 address)
{
    npk_u64 aligned = address & ~(npk_u64)7;
    npk_u64 shift = (address & 7) * 8;
    npk_u64 word = *((npk_u64 *)aligned);

    return (word >> shift) & 0xFF;
}

static void npk_store_byte(npk_u64 address, npk_u64 value)
{
    npk_u64 aligned = address & ~(npk_u64)7;
    npk_u64 shift = (address & 7) * 8;
    npk_u64 word = *((npk_u64 *)aligned);
    npk_u64 mask = (npk_u64)0xFF << shift;

    word = (word & ~mask) | ((value & 0xFF) << shift);
    *((npk_u64 *)aligned) = word;
}

int npk_device_init(void)
{
    struct NpkDeviceState *state = npk_device_state();
    struct NpkDeviceCapability *cap = npk_capabilities();
    npk_u64 i;

    for (i = 0; i < 18; i = i + 1) {
        *((npk_u64 *)state + i) = 0;
    }
    for (i = 0; i < NPK_DEVICE_INTERRUPT_SLOTS * 4; i = i + 1) {
        *((npk_u64 *)NPK_DEVICE_INTERRUPT_LOG_ADDRESS + i) = 0;
    }
    for (i = 0; i < NPK_DEVICE_CAPABILITY_SLOTS * 4; i = i + 1) {
        *((npk_u64 *)NPK_DEVICE_CAPABILITY_TABLE_ADDRESS + i) = 0;
    }

    npk_device_write_ie(0);
    npk_device_write_ip(0);
    npk_device_write_timecmp(NPK_U64_MAX);
    npk_device_write_ivec(npk_device_read_vbar());

    cap[0].kind = NPK_DEVICE_KIND_CONSOLE;
    cap[0].status = NPK_DEVICE_STATUS_SUPPORTED;
    cap[1].kind = NPK_DEVICE_KIND_TIMER;
    cap[1].status = NPK_DEVICE_STATUS_SUPPORTED;
    cap[2].kind = NPK_DEVICE_KIND_BLOCK;
    cap[2].status = NPK_DEVICE_STATUS_UNSUPPORTED;
    cap[3].kind = NPK_DEVICE_KIND_INPUT;
    cap[3].status = NPK_DEVICE_STATUS_UNSUPPORTED;
    cap[4].kind = NPK_DEVICE_KIND_NETWORK;
    cap[4].status = NPK_DEVICE_STATUS_UNSUPPORTED;

    state->magic = NPK_DEVICE_STATE_MAGIC;
    state->version = NPK_DEVICE_STATE_VERSION;
    state->init_status = 0;
    state->timer_interval = NPK_DEVICE_TIMER_INTERVAL;
    state->last_time = npk_device_read_time();
    state->last_timecmp = npk_device_read_timecmp();
    state->last_ie = npk_device_read_ie();
    state->last_ip = npk_device_read_ip();
    state->last_ivec = npk_device_read_ivec();
    return 0;
}

int npk_device_console_commit(npk_u64 scratch, npk_u64 sink, npk_u64 offset,
                              npk_u64 count)
{
    struct NpkDeviceState *state = npk_device_state();
    npk_u64 i;

    if (state->magic != NPK_DEVICE_STATE_MAGIC ||
            state->version != NPK_DEVICE_STATE_VERSION) {
        return 1;
    }
    for (i = 0; i < count; i = i + 1) {
        npk_store_byte(sink + offset + i, npk_load_byte(scratch + i));
    }
    state->console_calls = state->console_calls + 1;
    state->console_bytes = state->console_bytes + count;
    return 0;
}

int npk_device_interrupt(npk_u64 classification, npk_u64 ecause, npk_u64 epc)
{
    struct NpkDeviceState *state = npk_device_state();
    struct NpkDeviceInterrupt *record;
    npk_u64 source = ecause & 3;
    npk_u64 time;
    npk_u64 compare;
    npk_u64 ie;
    npk_u64 ip;
    npk_u64 ivec;

    if (state->magic != NPK_DEVICE_STATE_MAGIC ||
            state->version != NPK_DEVICE_STATE_VERSION) {
        return 1;
    }
    if (source > NPK_SOURCE_EXTERNAL) {
        return 1;
    }
    if (classification != source + NPK_CLASS_SOFTWARE_INTERRUPT) {
        return 1;
    }

    time = npk_device_read_time();
    compare = npk_device_read_timecmp();
    ie = npk_device_read_ie();
    ip = npk_device_read_ip();
    ivec = npk_device_read_ivec();

    state->interrupt_count = state->interrupt_count + 1;
    state->last_source = source;
    state->last_ecause = ecause;
    state->last_time = time;
    state->last_ie = ie;
    state->last_ip = ip;
    state->last_ivec = ivec;

    if (source == NPK_SOURCE_TIMER) {
        state->timer_interrupts = state->timer_interrupts + 1;
        compare = (time + state->timer_interval) & ~(npk_u64)3;
        npk_device_write_timecmp(compare);
        state->timer_reprograms = state->timer_reprograms + 1;
    } else if (source == NPK_SOURCE_SOFTWARE) {
        state->software_interrupts = state->software_interrupts + 1;
    } else {
        state->external_interrupts = state->external_interrupts + 1;
    }
    state->last_timecmp = compare;

    record = npk_interrupt_record(state->interrupt_count - 1);
    record->source = source;
    record->time = time;
    record->timecmp = compare;
    record->ie = ie;
    record->ip = ip;
    record->ivec = ivec;
    record->ecause = ecause;
    record->epc = epc;
    return 0;
}
