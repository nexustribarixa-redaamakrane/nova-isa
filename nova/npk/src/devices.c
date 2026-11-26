/*
 * NPK Milestone 7 device abstraction and timer processing.
 *
 * Nova ISA v1.0 has no MMIO, device tree, interrupt controller, framebuffer,
 * keyboard, disk, or network. This file therefore does not invent one. It owns
 * two capabilities the platform genuinely defines:
 *
 *   - a console sink reached through the existing SYS_WRITE trap path and a
 *     typed device write operation; bytes remain in the Milestone 6 write log;
 *   - the architectural one-shot timer and interrupt CSRs time, timecmp, ie,
 *     ip, and ivec, plus a bounded software timer table whose events are
 *     ordered by the emulator cycle counter and delivered as software events.
 *
 * Device registration, unregistration, queries, writes, and timer operations
 * are typed operations admitted to the Milestone 2 processing core. EXECUTE
 * never publishes backend state; COMMIT does. Unsupported kinds are rejected
 * by capability checks rather than represented by dormant drivers.
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

#define NPK_DEVICE_REQUEST_ADDRESS 0xA1390
#define NPK_DEVICE_RESPONSE_ADDRESS 0xA1400
#define NPK_DEVICE_OPERATION_STATE_ADDRESS 0xA1450
#define NPK_DEVICE_REGISTRY_ADDRESS 0xA14E0
#define NPK_DEVICE_SCRATCH_ADDRESS 0xA3A00

#define NPK_TIMER_TABLE_ADDRESS 0xA16E0
#define NPK_TIMER_EVENT_LOG_ADDRESS 0xA18E0

#define NPK_SYSCALL_WRITE_LOG_ADDRESS 0x9FE90
#define NPK_SYSCALL_WRITE_LOG_SIZE 256

#define NPK_DEVICE_OPERATION_STATE_MAGIC 0x4E504B4F
#define NPK_DEVICE_OPERATION_STATE_VERSION 1
#define NPK_DEVICE_REQUEST_MAGIC 0x4E504B46
#define NPK_DEVICE_RESPONSE_MAGIC 0x4E504B47

#define NPK_DEVICE_KIND_CONSOLE 1
#define NPK_DEVICE_KIND_TIMER 2
#define NPK_DEVICE_KIND_BLOCK 3
#define NPK_DEVICE_KIND_INPUT 4
#define NPK_DEVICE_KIND_NETWORK 5

#define NPK_DEVICE_STATUS_SUPPORTED 1
#define NPK_DEVICE_STATUS_UNSUPPORTED 2

#define NPK_DEVICE_CAP_WRITE 1
#define NPK_DEVICE_CAP_TIME 2

#define NPK_REGISTRY_FREE 0
#define NPK_REGISTRY_REGISTERED 1
#define NPK_REGISTRY_UNREGISTERED 2

#define NPK_TIMER_FREE 0
#define NPK_TIMER_PENDING 1
#define NPK_TIMER_EXPIRED 2
#define NPK_TIMER_DELIVERED 3
#define NPK_TIMER_CANCELLED 4

#define NPK_CLASS_SOFTWARE_INTERRUPT 8
#define NPK_CLASS_TIMER_INTERRUPT 9
#define NPK_CLASS_EXTERNAL_INTERRUPT 10

#define NPK_SOURCE_SOFTWARE 0
#define NPK_SOURCE_TIMER 1
#define NPK_SOURCE_EXTERNAL 2

#define NPK_OP_DEVICE_REGISTER 22
#define NPK_OP_DEVICE_UNREGISTER 23
#define NPK_OP_DEVICE_QUERY 24
#define NPK_OP_DEVICE_WRITE 25
#define NPK_OP_TIMER_CREATE 26
#define NPK_OP_TIMER_CANCEL 27
#define NPK_OP_TIMER_INSPECT 28
#define NPK_OP_TIMER_EXPIRE 29
#define NPK_OP_TIMER_DELIVER 30

#define NPK_REASON_NONE 0
#define NPK_REASON_DEVICE_BAD_REQUEST 57
#define NPK_REASON_DEVICE_BAD_KIND 58
#define NPK_REASON_DEVICE_UNSUPPORTED 59
#define NPK_REASON_DEVICE_NOT_FOUND 60
#define NPK_REASON_DEVICE_DUPLICATE 61
#define NPK_REASON_DEVICE_EXHAUSTED 62
#define NPK_REASON_DEVICE_STATE 63
#define NPK_REASON_DEVICE_CAPABILITY 64
#define NPK_REASON_DEVICE_BAD_PARAMETER 65
#define NPK_REASON_DEVICE_OWNER 66
#define NPK_REASON_DEVICE_OBJECT 67
#define NPK_REASON_TIMER_BAD_REQUEST 68
#define NPK_REASON_TIMER_NOT_FOUND 69
#define NPK_REASON_TIMER_DUPLICATE 70
#define NPK_REASON_TIMER_EXHAUSTED 71
#define NPK_REASON_TIMER_STATE 72
#define NPK_REASON_TIMER_NOT_DUE 73
#define NPK_REASON_TIMER_BAD_INTERVAL 74
#define NPK_REASON_TIMER_EVENT 75
#define NPK_REASON_TIMER_UNSUPPORTED 76
#define NPK_REASON_DEVICE_BACKEND 77

#define NPK_ERROR_NONE 0
#define NPK_ERROR_BAD_ARGUMENT 1
#define NPK_ERROR_UNSUPPORTED 2
#define NPK_ERROR_NOT_FOUND 3
#define NPK_ERROR_NO_RESOURCE 4

#define NPK_REQUEST_OK 0
#define NPK_REQUEST_BAD_REQUEST 1
#define NPK_REQUEST_BAD_OP 2
#define NPK_REQUEST_ADMIT_FAIL 3
#define NPK_REQUEST_ADVANCE_FAIL 4
#define NPK_REQUEST_BAD_OUTCOME 5
#define NPK_REQUEST_REJECTED 6
#define NPK_REQUEST_FAILED 7

#define NPK_OUTCOME_PENDING 1
#define NPK_OUTCOME_COMPLETE 2
#define NPK_OUTCOME_REJECTED 3
#define NPK_OUTCOME_FAILED 4

#define NPK_ACCESS_READ 1
#define NPK_MEMORY_REASON_NONE 0
#define NPK_HANDLE_OWNERS 2

#define NPK_DEVICE_TIMER_INTERVAL 16384
#define NPK_MAX_TIMER_INTERVAL 0x100000
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

struct NpkDeviceOperationState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 init_status;
    npk_u64 last_status;
    npk_u64 last_operation;
    npk_u64 last_target;
    npk_u64 last_reason;
    npk_u64 last_outcome;
    npk_u64 last_receipt;
    npk_u64 device_operations;
    npk_u64 timer_operations;
    npk_u64 registered_count;
    npk_u64 timer_count;
    npk_u64 timer_events;
    npk_u64 timer_expirations;
    npk_u64 timer_deliveries;
    npk_u64 timer_cancellations;
    npk_u64 next_event_id;
};

struct NpkDeviceRegistryEntry {
    npk_u64 state;
    npk_u64 id;
    npk_u64 kind;
    npk_u64 capability;
    npk_u64 owner;
    npk_u64 object_handle;
    npk_u64 operations;
    npk_u64 last_result;
};

struct NpkTimerEntry {
    npk_u64 state;
    npk_u64 id;
    npk_u64 owner;
    npk_u64 deadline;
    npk_u64 interval;
    npk_u64 event;
    npk_u64 created;
    npk_u64 scratch;
};

struct NpkTimerEvent {
    npk_u64 event_id;
    npk_u64 timer_id;
    npk_u64 deadline;
    npk_u64 delivered;
};

struct NpkDeviceRequest {
    npk_u64 magic;
    npk_u64 operation;
    npk_u64 id;
    npk_u64 target;
    npk_u64 owner;
    npk_u64 object;
    npk_u64 kind;
    npk_u64 capability;
    npk_u64 arg0;
    npk_u64 arg1;
    npk_u64 arg2;
    npk_u64 arg3;
    npk_u64 aux;
};

struct NpkDeviceResponse {
    npk_u64 magic;
    npk_u64 status;
    npk_u64 operation;
    npk_u64 reason;
    npk_u64 result;
    npk_u64 target;
    npk_u64 receipt_index;
    npk_u64 slot;
    npk_u64 error;
    npk_u64 aux;
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

extern int npk_core_admit(npk_u64 type, npk_u64 id, npk_u64 input,
                          npk_u64 *out_slot);
extern int npk_core_advance(npk_u64 slot);
extern int npk_core_release(npk_u64 slot);
extern int npk_core_operation_outcome(npk_u64 slot);
extern npk_u64 npk_core_operation_result(npk_u64 slot);
extern npk_u64 npk_core_operation_reason(npk_u64 slot);
extern npk_u64 npk_core_operation_receipt_index(npk_u64 slot);
extern npk_u64 npk_core_operation_device_evidence(npk_u64 slot);
extern npk_u64 npk_core_operation_device_error(npk_u64 slot);

extern npk_u64 npk_object_id_for_handle(npk_u64 handle, npk_u64 owner);
extern npk_u64 npk_memory_user_physical(npk_u64 address, npk_u64 access,
                                        npk_u64 *out_physical);
extern npk_u64 npk_syscall_write_log_count(void);
extern void npk_syscall_write_log_advance(npk_u64 count);

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

static struct NpkDeviceOperationState *npk_operation_state(void)
{
    return (struct NpkDeviceOperationState *)NPK_DEVICE_OPERATION_STATE_ADDRESS;
}

static struct NpkDeviceRegistryEntry *npk_registry(npk_u64 index)
{
    return (struct NpkDeviceRegistryEntry *)(NPK_DEVICE_REGISTRY_ADDRESS +
        index * sizeof(struct NpkDeviceRegistryEntry));
}

static struct NpkTimerEntry *npk_timer(npk_u64 index)
{
    return (struct NpkTimerEntry *)(NPK_TIMER_TABLE_ADDRESS +
        index * sizeof(struct NpkTimerEntry));
}

static struct NpkTimerEvent *npk_timer_event(npk_u64 index)
{
    return (struct NpkTimerEvent *)(NPK_TIMER_EVENT_LOG_ADDRESS +
        index * sizeof(struct NpkTimerEvent));
}

static struct NpkDeviceRequest *npk_request(void)
{
    return (struct NpkDeviceRequest *)NPK_DEVICE_REQUEST_ADDRESS;
}

static struct NpkDeviceResponse *npk_response(void)
{
    return (struct NpkDeviceResponse *)NPK_DEVICE_RESPONSE_ADDRESS;
}

static npk_u64 *npk_scratch(void)
{
    return (npk_u64 *)NPK_DEVICE_SCRATCH_ADDRESS;
}

static void npk_clear_words(npk_u64 *base, npk_u64 count)
{
    npk_u64 i;

    for (i = 0; i < count; i = i + 1) {
        base[i] = 0;
    }
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

static int npk_supported_kind(npk_u64 kind)
{
    return kind == NPK_DEVICE_KIND_CONSOLE || kind == NPK_DEVICE_KIND_TIMER;
}

static npk_u64 npk_capability_for_kind(npk_u64 kind)
{
    if (kind == NPK_DEVICE_KIND_CONSOLE) {
        return NPK_DEVICE_CAP_WRITE;
    }
    if (kind == NPK_DEVICE_KIND_TIMER) {
        return NPK_DEVICE_CAP_TIME;
    }
    return 0;
}

static npk_u64 npk_capability_status(npk_u64 kind)
{
    struct NpkDeviceCapability *cap = npk_capabilities();
    npk_u64 i;

    for (i = 0; i < NPK_DEVICE_CAPABILITY_SLOTS; i = i + 1) {
        if (cap[i].kind == kind) {
            return cap[i].status;
        }
    }
    return 0;
}

static npk_u64 npk_find_free_registry(void)
{
    npk_u64 i;

    for (i = 0; i < 8; i = i + 1) {
        if (npk_registry(i)->state == NPK_REGISTRY_FREE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_device(npk_u64 id)
{
    npk_u64 i;

    if (id == 0) {
        return NPK_NO_INDEX;
    }
    for (i = 0; i < 8; i = i + 1) {
        if (npk_registry(i)->state == NPK_REGISTRY_REGISTERED &&
                npk_registry(i)->id == id) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_any_device_id(npk_u64 id)
{
    npk_u64 i;

    for (i = 0; i < 8; i = i + 1) {
        if (npk_registry(i)->state != NPK_REGISTRY_FREE &&
                npk_registry(i)->id == id) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_free_timer(void)
{
    npk_u64 i;

    for (i = 0; i < 8; i = i + 1) {
        if (npk_timer(i)->state == NPK_TIMER_FREE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_timer(npk_u64 id)
{
    npk_u64 i;

    if (id == 0) {
        return NPK_NO_INDEX;
    }
    for (i = 0; i < 8; i = i + 1) {
        if (npk_timer(i)->state != NPK_TIMER_FREE &&
                npk_timer(i)->id == id) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static int npk_owner_ok(npk_u64 owner)
{
    return owner <= NPK_HANDLE_OWNERS;
}

static int npk_object_ok(npk_u64 owner, npk_u64 handle)
{
    if (handle == 0) {
        return 1;
    }
    if (owner == 0) {
        return 0;
    }
    return npk_object_id_for_handle(handle, owner) != NPK_NO_INDEX;
}

static int npk_association_ok(npk_u64 owner, npk_u64 handle,
                              npk_u64 device_owner, npk_u64 device_handle)
{
    if (owner != 0 && owner != device_owner) {
        return 0;
    }
    if (handle != 0 && handle != device_handle) {
        return 0;
    }
    return 1;
}

static npk_u64 npk_error_for_reason(npk_u64 reason)
{
    if (reason == NPK_REASON_DEVICE_UNSUPPORTED ||
            reason == NPK_REASON_TIMER_UNSUPPORTED) {
        return NPK_ERROR_UNSUPPORTED;
    }
    if (reason == NPK_REASON_DEVICE_NOT_FOUND ||
            reason == NPK_REASON_TIMER_NOT_FOUND ||
            reason == NPK_REASON_DEVICE_OBJECT) {
        return NPK_ERROR_NOT_FOUND;
    }
    if (reason == NPK_REASON_DEVICE_EXHAUSTED ||
            reason == NPK_REASON_TIMER_EXHAUSTED ||
            reason == NPK_REASON_DEVICE_STATE ||
            reason == NPK_REASON_DEVICE_BACKEND) {
        return NPK_ERROR_NO_RESOURCE;
    }
    return NPK_ERROR_BAD_ARGUMENT;
}

static npk_u64 npk_admit_reason(int status)
{
    if (status == NPK_ERR_BAD_ID) {
        return NPK_REASON_DEVICE_BAD_REQUEST;
    }
    if (status == NPK_ERR_BAD_TYPE) {
        return NPK_REASON_DEVICE_UNSUPPORTED;
    }
    if (status == NPK_ERR_DUPLICATE_ID) {
        return NPK_REASON_DEVICE_DUPLICATE;
    }
    if (status == NPK_ERR_NO_SLOT) {
        return NPK_REASON_DEVICE_EXHAUSTED;
    }
    if (status == NPK_ERR_UNIT_UNAVAILABLE) {
        return NPK_REASON_DEVICE_STATE;
    }
    if (status == NPK_ERR_NO_RECEIPT) {
        return NPK_REASON_DEVICE_EXHAUSTED;
    }
    return NPK_REASON_DEVICE_BAD_REQUEST;
}

static npk_u64 npk_validate_user_range(npk_u64 address, npk_u64 count)
{
    npk_u64 physical = 0;
    npk_u64 i;

    if (address == 0 || count == 0 || count > 64) {
        return NPK_REASON_DEVICE_BAD_PARAMETER;
    }
    if (address > NPK_U64_MAX - (count - 1)) {
        return NPK_REASON_DEVICE_BAD_PARAMETER;
    }
    for (i = 0; i < count; i = i + 1) {
        if (npk_memory_user_physical(address + i, NPK_ACCESS_READ,
                                     &physical) != NPK_MEMORY_REASON_NONE) {
            return NPK_REASON_DEVICE_BAD_PARAMETER;
        }
    }
    return NPK_REASON_NONE;
}

static npk_u64 npk_copy_user_bytes(npk_u64 address, npk_u64 count)
{
    npk_u64 physical = 0;
    npk_u64 reason;
    npk_u64 i;

    reason = npk_validate_user_range(address, count);
    if (reason != NPK_REASON_NONE) {
        return reason;
    }
    for (i = 0; i < count; i = i + 1) {
        npk_memory_user_physical(address + i, NPK_ACCESS_READ, &physical);
        npk_store_byte((npk_u64)npk_scratch() + i, npk_load_byte(physical));
    }
    return NPK_REASON_NONE;
}

int npk_device_init(void)
{
    struct NpkDeviceState *state = npk_device_state();
    struct NpkDeviceCapability *cap = npk_capabilities();
    struct NpkDeviceOperationState *ops = npk_operation_state();
    struct NpkDeviceRequest *request = npk_request();
    struct NpkDeviceResponse *response = npk_response();
    npk_u64 i;

    npk_clear_words((npk_u64 *)state, 18);
    npk_clear_words((npk_u64 *)NPK_DEVICE_INTERRUPT_LOG_ADDRESS,
                    NPK_DEVICE_INTERRUPT_SLOTS * 4);
    npk_clear_words((npk_u64 *)NPK_DEVICE_CAPABILITY_TABLE_ADDRESS,
                    NPK_DEVICE_CAPABILITY_SLOTS * 4);
    npk_clear_words((npk_u64 *)ops, 18);
    npk_clear_words((npk_u64 *)NPK_DEVICE_REGISTRY_ADDRESS, 8 * 8);
    npk_clear_words((npk_u64 *)NPK_TIMER_TABLE_ADDRESS, 8 * 8);
    npk_clear_words((npk_u64 *)NPK_TIMER_EVENT_LOG_ADDRESS, 8 * 4);
    npk_clear_words((npk_u64 *)request, 13);
    npk_clear_words((npk_u64 *)response, 10);
    npk_clear_words(npk_scratch(), 8);

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

    ops->magic = NPK_DEVICE_OPERATION_STATE_MAGIC;
    ops->version = NPK_DEVICE_OPERATION_STATE_VERSION;
    ops->init_status = 0;
    ops->last_status = NPK_REQUEST_OK;
    ops->last_receipt = NPK_NO_INDEX;
    ops->next_event_id = 1;
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
    if (count > 64 || offset + count > NPK_SYSCALL_WRITE_LOG_SIZE) {
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

static int npk_request_matches(struct NpkOperation *op,
                               struct NpkDeviceRequest *request)
{
    if (op->input != NPK_DEVICE_REQUEST_ADDRESS ||
            request->magic != NPK_DEVICE_REQUEST_MAGIC ||
            request->id != op->id ||
            request->operation != op->type ||
            request->target != op->device_target ||
            request->kind != op->device_kind ||
            request->owner != op->device_owner ||
            request->object != op->device_object ||
            request->capability != op->device_capability) {
        return 0;
    }
    if (request->arg0 != op->device_arg[0] ||
            request->arg1 != op->device_arg[1] ||
            request->arg2 != op->device_arg[2] ||
            request->arg3 != op->device_arg[3]) {
        return 0;
    }
    return 1;
}

static npk_u64 npk_validate_device_register(struct NpkOperation *op)
{
    npk_u64 i;

    if (!npk_supported_kind(op->device_kind) ||
            npk_capability_status(op->device_kind) !=
            NPK_DEVICE_STATUS_SUPPORTED) {
        op->device_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_DEVICE_UNSUPPORTED;
    }
    if (op->device_capability != npk_capability_for_kind(op->device_kind)) {
        op->device_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_DEVICE_CAPABILITY;
    }
    if (npk_find_any_device_id(op->device_target) != NPK_NO_INDEX) {
        op->device_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_DEVICE_DUPLICATE;
    }
    if (npk_find_free_registry() == NPK_NO_INDEX) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_DEVICE_EXHAUSTED;
    }
    for (i = 0; i < 4; i = i + 1) {
        if (op->device_arg[i] != 0) {
            op->device_error = NPK_ERROR_BAD_ARGUMENT;
            return NPK_REASON_DEVICE_BAD_REQUEST;
        }
    }
    op->device_evidence = op->device_capability;
    return NPK_REASON_NONE;
}

static npk_u64 npk_validate_device_operation(struct NpkOperation *op)
{
    struct NpkDeviceRegistryEntry *device;
    npk_u64 index;
    npk_u64 i;
    npk_u64 count;

    index = npk_find_device(op->device_target);
    if (index == NPK_NO_INDEX) {
        op->device_error = NPK_ERROR_NOT_FOUND;
        return NPK_REASON_DEVICE_NOT_FOUND;
    }
    device = npk_registry(index);
    if (device->kind != op->device_kind ||
            npk_association_ok(op->device_owner, op->device_object,
                               device->owner, device->object_handle) == 0) {
        op->device_error = NPK_ERROR_NOT_FOUND;
        return NPK_REASON_DEVICE_OBJECT;
    }
    if (op->device_capability != device->capability) {
        op->device_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_DEVICE_CAPABILITY;
    }
    if (op->type == NPK_OP_DEVICE_UNREGISTER) {
        if (device->state != NPK_REGISTRY_REGISTERED) {
            op->device_error = NPK_ERROR_NO_RESOURCE;
            return NPK_REASON_DEVICE_STATE;
        }
        for (i = 0; i < 4; i = i + 1) {
            if (op->device_arg[i] != 0) {
                op->device_error = NPK_ERROR_BAD_ARGUMENT;
                return NPK_REASON_DEVICE_BAD_REQUEST;
            }
        }
        op->device_evidence = device->kind;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_DEVICE_QUERY) {
        for (i = 0; i < 4; i = i + 1) {
            if (op->device_arg[i] != 0) {
                op->device_error = NPK_ERROR_BAD_ARGUMENT;
                return NPK_REASON_DEVICE_BAD_REQUEST;
            }
        }
        op->device_evidence = device->capability;
        return NPK_REASON_NONE;
    }
    if (device->capability != NPK_DEVICE_CAP_WRITE) {
        op->device_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_DEVICE_UNSUPPORTED;
    }
    count = op->device_arg[1];
    if (npk_syscall_write_log_count() + count > NPK_SYSCALL_WRITE_LOG_SIZE) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_DEVICE_BACKEND;
    }
    if (npk_validate_user_range(op->device_arg[0], count) !=
            NPK_REASON_NONE) {
        op->device_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_DEVICE_BAD_PARAMETER;
    }
    if (op->device_arg[2] != 0 || op->device_arg[3] != 0) {
        op->device_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_DEVICE_BAD_REQUEST;
    }
    op->device_evidence = count;
    return NPK_REASON_NONE;
}

static npk_u64 npk_validate_timer_create(struct NpkOperation *op)
{
    if (op->device_kind != NPK_DEVICE_KIND_TIMER ||
            op->device_capability != NPK_DEVICE_CAP_TIME ||
            npk_capability_status(NPK_DEVICE_KIND_TIMER) !=
            NPK_DEVICE_STATUS_SUPPORTED) {
        op->device_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_TIMER_UNSUPPORTED;
    }
    if (npk_find_timer(op->device_target) != NPK_NO_INDEX) {
        op->device_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_TIMER_DUPLICATE;
    }
    if (npk_find_free_timer() == NPK_NO_INDEX) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_TIMER_EXHAUSTED;
    }
    if (op->device_arg[0] == 0 ||
            op->device_arg[0] > NPK_MAX_TIMER_INTERVAL ||
            npk_device_read_time() > NPK_U64_MAX - op->device_arg[0]) {
        op->device_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_TIMER_BAD_INTERVAL;
    }
    if (op->device_arg[1] != 0 || op->device_arg[2] != 0 ||
            op->device_arg[3] != 0) {
        op->device_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_TIMER_BAD_REQUEST;
    }
    op->device_evidence = op->device_arg[0];
    return NPK_REASON_NONE;
}

static npk_u64 npk_validate_timer_operation(struct NpkOperation *op)
{
    struct NpkTimerEntry *timer;
    npk_u64 index;
    npk_u64 i;

    if (op->device_capability != NPK_DEVICE_CAP_TIME) {
        op->device_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_TIMER_UNSUPPORTED;
    }
    index = npk_find_timer(op->device_target);
    if (index == NPK_NO_INDEX) {
        op->device_error = NPK_ERROR_NOT_FOUND;
        return NPK_REASON_TIMER_NOT_FOUND;
    }
    timer = npk_timer(index);
    if (op->device_owner != 0 && op->device_owner != timer->owner) {
        op->device_error = NPK_ERROR_NOT_FOUND;
        return NPK_REASON_TIMER_NOT_FOUND;
    }
    for (i = 0; i < 4; i = i + 1) {
        if (op->device_arg[i] != 0) {
            op->device_error = NPK_ERROR_BAD_ARGUMENT;
            return NPK_REASON_TIMER_BAD_REQUEST;
        }
    }
    if (op->type == NPK_OP_TIMER_CANCEL &&
            timer->state != NPK_TIMER_PENDING) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_TIMER_STATE;
    }
    if (op->type == NPK_OP_TIMER_DELIVER &&
            timer->state != NPK_TIMER_EXPIRED) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_TIMER_STATE;
    }
    op->device_evidence = timer->id;
    return NPK_REASON_NONE;
}

static npk_u64 npk_validate_timer_expire(struct NpkOperation *op)
{
    struct NpkTimerEntry *timer;
    npk_u64 now;
    npk_u64 found;
    npk_u64 i;

    if (op->device_capability != NPK_DEVICE_CAP_TIME) {
        op->device_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_TIMER_UNSUPPORTED;
    }
    if (npk_operation_state()->timer_events >= 8) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_TIMER_EVENT;
    }
    now = npk_device_read_time();
    found = 0;
    for (i = 0; i < 8; i = i + 1) {
        timer = npk_timer(i);
        if (timer->state != NPK_TIMER_PENDING || timer->deadline > now) {
            continue;
        }
        if (op->device_target != 0 && timer->id != op->device_target) {
            continue;
        }
        found = 1;
    }
    if (found == 0) {
        op->device_error = NPK_ERROR_NOT_FOUND;
        return NPK_REASON_TIMER_NOT_DUE;
    }
    for (i = 0; i < 4; i = i + 1) {
        if (op->device_arg[i] != 0) {
            op->device_error = NPK_ERROR_BAD_ARGUMENT;
            return NPK_REASON_TIMER_BAD_REQUEST;
        }
    }
    op->device_evidence = op->device_target;
    return NPK_REASON_NONE;
}

npk_u64 npk_device_validate(struct NpkOperation *op)
{
    struct NpkDeviceOperationState *ops = npk_operation_state();
    struct NpkDeviceRequest *request = npk_request();

    if (npk_request_matches(op, request) == 0) {
        op->device_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_DEVICE_BAD_REQUEST;
    }
    if (ops->magic != NPK_DEVICE_OPERATION_STATE_MAGIC ||
            ops->version != NPK_DEVICE_OPERATION_STATE_VERSION ||
            ops->init_status != 0) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_DEVICE_STATE;
    }
    if (!npk_owner_ok(op->device_owner) ||
            npk_object_ok(op->device_owner, op->device_object) == 0) {
        op->device_error = NPK_ERROR_NOT_FOUND;
        return NPK_REASON_DEVICE_OBJECT;
    }
    if (op->type == NPK_OP_DEVICE_REGISTER) {
        return npk_validate_device_register(op);
    }
    if (op->type == NPK_OP_DEVICE_UNREGISTER ||
            op->type == NPK_OP_DEVICE_QUERY ||
            op->type == NPK_OP_DEVICE_WRITE) {
        return npk_validate_device_operation(op);
    }
    if (op->type == NPK_OP_TIMER_CREATE) {
        return npk_validate_timer_create(op);
    }
    if (op->type == NPK_OP_TIMER_CANCEL ||
            op->type == NPK_OP_TIMER_INSPECT ||
            op->type == NPK_OP_TIMER_DELIVER) {
        return npk_validate_timer_operation(op);
    }
    if (op->type == NPK_OP_TIMER_EXPIRE) {
        return npk_validate_timer_expire(op);
    }
    op->device_error = NPK_ERROR_UNSUPPORTED;
    return NPK_REASON_DEVICE_UNSUPPORTED;
}

npk_u64 npk_device_execute(struct NpkOperation *op)
{
    struct NpkDeviceRegistryEntry *device;
    struct NpkTimerEntry *timer;
    npk_u64 index;
    npk_u64 now;
    npk_u64 chosen;
    npk_u64 chosen_deadline;
    npk_u64 chosen_id;
    npk_u64 reason;
    npk_u64 i;

    if (op->type == NPK_OP_DEVICE_REGISTER) {
        op->provisional = op->device_target;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_DEVICE_UNREGISTER) {
        index = npk_find_device(op->device_target);
        if (index == NPK_NO_INDEX) {
            return NPK_REASON_DEVICE_NOT_FOUND;
        }
        op->provisional = index;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_DEVICE_QUERY) {
        index = npk_find_device(op->device_target);
        if (index == NPK_NO_INDEX) {
            return NPK_REASON_DEVICE_NOT_FOUND;
        }
        device = npk_registry(index);
        op->provisional = (device->state << 32) | device->kind;
        op->device_evidence = device->capability;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_DEVICE_WRITE) {
        reason = npk_copy_user_bytes(op->device_arg[0], op->device_arg[1]);
        if (reason != NPK_REASON_NONE) {
            op->device_error = NPK_ERROR_BAD_ARGUMENT;
            return reason;
        }
        op->provisional = op->device_arg[1];
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TIMER_CREATE) {
        now = npk_device_read_time();
        op->provisional = now + op->device_arg[0];
        op->device_arg[2] = now;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TIMER_CANCEL ||
            op->type == NPK_OP_TIMER_INSPECT ||
            op->type == NPK_OP_TIMER_DELIVER) {
        index = npk_find_timer(op->device_target);
        if (index == NPK_NO_INDEX) {
            return NPK_REASON_TIMER_NOT_FOUND;
        }
        timer = npk_timer(index);
        if (op->type == NPK_OP_TIMER_INSPECT) {
            op->provisional = timer->state;
            op->device_evidence = timer->deadline;
        } else {
            op->provisional = index;
            op->device_evidence = timer->id;
        }
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TIMER_EXPIRE) {
        now = npk_device_read_time();
        chosen = NPK_NO_INDEX;
        chosen_deadline = 0;
        chosen_id = 0;
        for (i = 0; i < 8; i = i + 1) {
            timer = npk_timer(i);
            if (timer->state != NPK_TIMER_PENDING ||
                    timer->deadline > now) {
                continue;
            }
            if (op->device_target != 0 &&
                    timer->id != op->device_target) {
                continue;
            }
            if (chosen == NPK_NO_INDEX ||
                    timer->deadline < chosen_deadline ||
                    (timer->deadline == chosen_deadline &&
                     timer->id < chosen_id)) {
                chosen = i;
                chosen_deadline = timer->deadline;
                chosen_id = timer->id;
            }
        }
        if (chosen == NPK_NO_INDEX) {
            op->device_error = NPK_ERROR_NOT_FOUND;
            return NPK_REASON_TIMER_NOT_DUE;
        }
        op->provisional = chosen;
        op->device_evidence = chosen_id;
        op->device_arg[0] = npk_operation_state()->next_event_id;
        op->device_arg[1] = now;
        return NPK_REASON_NONE;
    }
    op->device_error = NPK_ERROR_UNSUPPORTED;
    return NPK_REASON_DEVICE_UNSUPPORTED;
}

static npk_u64 npk_commit_device_operation(struct NpkOperation *op)
{
    struct NpkDeviceOperationState *ops = npk_operation_state();
    struct NpkDeviceRegistryEntry *device;
    npk_u64 index;
    npk_u64 count;
    npk_u64 log_count;

    if (op->type == NPK_OP_DEVICE_REGISTER) {
        index = npk_find_free_registry();
        if (index == NPK_NO_INDEX) {
            op->device_error = NPK_ERROR_NO_RESOURCE;
            return NPK_REASON_DEVICE_EXHAUSTED;
        }
        device = npk_registry(index);
        device->state = NPK_REGISTRY_REGISTERED;
        device->id = op->device_target;
        device->kind = op->device_kind;
        device->capability = op->device_capability;
        device->owner = op->device_owner;
        device->object_handle = op->device_object;
        device->operations = 0;
        device->last_result = 0;
        ops->registered_count = ops->registered_count + 1;
        op->result = op->device_target;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_DEVICE_UNREGISTER) {
        index = npk_find_device(op->device_target);
        if (index == NPK_NO_INDEX) {
            return NPK_REASON_DEVICE_NOT_FOUND;
        }
        device = npk_registry(index);
        device->state = NPK_REGISTRY_UNREGISTERED;
        device->owner = 0;
        device->object_handle = 0;
        ops->registered_count = ops->registered_count - 1;
        op->result = op->device_target;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_DEVICE_QUERY) {
        op->result = op->provisional;
        return NPK_REASON_NONE;
    }
    count = op->provisional;
    log_count = npk_syscall_write_log_count();
    if (count == 0 || count > 64 ||
            log_count + count > NPK_SYSCALL_WRITE_LOG_SIZE) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_DEVICE_BACKEND;
    }
    if (npk_device_console_commit((npk_u64)npk_scratch(),
                                  NPK_SYSCALL_WRITE_LOG_ADDRESS,
                                  log_count, count) != 0) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_DEVICE_BACKEND;
    }
    npk_syscall_write_log_advance(count);
    index = npk_find_device(op->device_target);
    if (index != NPK_NO_INDEX) {
        npk_registry(index)->operations = npk_registry(index)->operations + 1;
        npk_registry(index)->last_result = count;
    }
    op->result = count;
    return NPK_REASON_NONE;
}

static npk_u64 npk_commit_timer_create(struct NpkOperation *op)
{
    struct NpkDeviceOperationState *ops = npk_operation_state();
    struct NpkTimerEntry *timer;
    npk_u64 index;

    index = npk_find_free_timer();
    if (index == NPK_NO_INDEX) {
        op->device_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_TIMER_EXHAUSTED;
    }
    timer = npk_timer(index);
    timer->state = NPK_TIMER_PENDING;
    timer->id = op->device_target;
    timer->owner = op->device_owner;
    timer->deadline = op->provisional;
    timer->interval = op->device_arg[0];
    timer->event = 0;
    timer->created = op->device_arg[2];
    timer->scratch = 0;
    ops->timer_count = ops->timer_count + 1;
    op->result = op->device_target;
    return NPK_REASON_NONE;
}

static npk_u64 npk_commit_timer_expire(struct NpkOperation *op)
{
    struct NpkDeviceOperationState *ops = npk_operation_state();
    struct NpkTimerEntry *timer;
    struct NpkTimerEvent *event;
    npk_u64 index;
    npk_u64 event_id;

    index = op->provisional;
    if (index >= 8) {
        return NPK_REASON_TIMER_NOT_FOUND;
    }
    timer = npk_timer(index);
    if (timer->state != NPK_TIMER_PENDING) {
        return NPK_REASON_TIMER_STATE;
    }
    event_id = op->device_arg[0];
    timer->state = NPK_TIMER_EXPIRED;
    timer->event = event_id;
    event = npk_timer_event((event_id - 1) & 7);
    event->event_id = event_id;
    event->timer_id = timer->id;
    event->deadline = timer->deadline;
    event->delivered = 0;
    ops->next_event_id = event_id + 1;
    ops->timer_events = ops->timer_events + 1;
    ops->timer_expirations = ops->timer_expirations + 1;
    op->result = timer->id;
    return NPK_REASON_NONE;
}

static npk_u64 npk_commit_timer_deliver(struct NpkOperation *op)
{
    struct NpkDeviceOperationState *ops = npk_operation_state();
    struct NpkTimerEntry *timer;
    struct NpkTimerEvent *event;
    npk_u64 index;

    index = npk_find_timer(op->device_target);
    if (index == NPK_NO_INDEX) {
        return NPK_REASON_TIMER_NOT_FOUND;
    }
    timer = npk_timer(index);
    if (timer->state != NPK_TIMER_EXPIRED) {
        return NPK_REASON_TIMER_STATE;
    }
    timer->state = NPK_TIMER_DELIVERED;
    event = npk_timer_event((timer->event - 1) & 7);
    if (event->event_id == timer->event) {
        event->delivered = 1;
    }
    ops->timer_deliveries = ops->timer_deliveries + 1;
    op->result = timer->id;
    return NPK_REASON_NONE;
}

npk_u64 npk_device_commit(struct NpkOperation *op)
{
    struct NpkDeviceOperationState *ops = npk_operation_state();
    npk_u64 index;

    if (op->type == NPK_OP_DEVICE_REGISTER ||
            op->type == NPK_OP_DEVICE_UNREGISTER ||
            op->type == NPK_OP_DEVICE_QUERY ||
            op->type == NPK_OP_DEVICE_WRITE) {
        return npk_commit_device_operation(op);
    }
    if (op->type == NPK_OP_TIMER_CREATE) {
        return npk_commit_timer_create(op);
    }
    if (op->type == NPK_OP_TIMER_CANCEL) {
        index = npk_find_timer(op->device_target);
        if (index == NPK_NO_INDEX) {
            return NPK_REASON_TIMER_NOT_FOUND;
        }
        npk_timer(index)->state = NPK_TIMER_CANCELLED;
        ops->timer_cancellations = ops->timer_cancellations + 1;
        op->result = op->device_target;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TIMER_INSPECT) {
        op->result = op->provisional;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TIMER_EXPIRE) {
        return npk_commit_timer_expire(op);
    }
    if (op->type == NPK_OP_TIMER_DELIVER) {
        return npk_commit_timer_deliver(op);
    }
    op->device_error = NPK_ERROR_UNSUPPORTED;
    return NPK_REASON_DEVICE_UNSUPPORTED;
}

int npk_device_request(void)
{
    struct NpkDeviceOperationState *ops = npk_operation_state();
    struct NpkDeviceRequest *request = npk_request();
    struct NpkDeviceResponse *response = npk_response();
    npk_u64 args[4];
    npk_u64 slot = NPK_NO_INDEX;
    npk_u64 type;
    npk_u64 reason = NPK_REASON_NONE;
    npk_u64 outcome = NPK_OUTCOME_PENDING;
    int status;

    npk_clear_words((npk_u64 *)response, 10);
    response->magic = NPK_DEVICE_RESPONSE_MAGIC;
    response->operation = request->operation;
    response->target = request->target;
    response->receipt_index = NPK_NO_INDEX;
    response->slot = NPK_NO_INDEX;
    ops->last_status = NPK_REQUEST_OK;
    ops->last_operation = request->operation;
    ops->last_target = request->target;
    ops->last_reason = NPK_REASON_NONE;
    ops->last_outcome = NPK_OUTCOME_PENDING;
    ops->last_receipt = NPK_NO_INDEX;

    if (request->magic != NPK_DEVICE_REQUEST_MAGIC || request->id == 0 ||
            ops->magic != NPK_DEVICE_OPERATION_STATE_MAGIC ||
            ops->version != NPK_DEVICE_OPERATION_STATE_VERSION ||
            ops->init_status != 0) {
        ops->last_status = NPK_REQUEST_BAD_REQUEST;
        response->status = NPK_REQUEST_BAD_REQUEST;
        response->reason = NPK_REASON_DEVICE_BAD_REQUEST;
        response->error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REQUEST_BAD_REQUEST;
    }
    type = request->operation;
    if (type < NPK_OP_DEVICE_REGISTER || type > NPK_OP_TIMER_DELIVER) {
        ops->last_status = NPK_REQUEST_BAD_OP;
        response->status = NPK_REQUEST_BAD_OP;
        response->reason = NPK_REASON_DEVICE_UNSUPPORTED;
        response->error = NPK_ERROR_UNSUPPORTED;
        return NPK_REQUEST_BAD_OP;
    }
    if (request->target == 0 && type != NPK_OP_TIMER_EXPIRE) {
        ops->last_status = NPK_REQUEST_BAD_REQUEST;
        response->status = NPK_REQUEST_BAD_REQUEST;
        response->reason = NPK_REASON_DEVICE_BAD_REQUEST;
        response->error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REQUEST_BAD_REQUEST;
    }
    if (!npk_owner_ok(request->owner) ||
            npk_object_ok(request->owner, request->object) == 0) {
        ops->last_status = NPK_REQUEST_BAD_REQUEST;
        response->status = NPK_REQUEST_BAD_REQUEST;
        response->reason = NPK_REASON_DEVICE_OBJECT;
        response->error = NPK_ERROR_NOT_FOUND;
        return NPK_REQUEST_BAD_REQUEST;
    }

    args[0] = request->arg0;
    args[1] = request->arg1;
    args[2] = request->arg2;
    args[3] = request->arg3;
    status = npk_core_admit(type, request->id, NPK_DEVICE_REQUEST_ADDRESS,
                            &slot);
    if (status != 0) {
        ops->last_status = NPK_REQUEST_ADMIT_FAIL;
        response->status = NPK_REQUEST_ADMIT_FAIL;
        response->reason = npk_admit_reason(status);
        response->error = npk_error_for_reason(response->reason);
        ops->last_reason = response->reason;
        return NPK_REQUEST_ADMIT_FAIL;
    }
    npk_core_set_device_arguments(slot, request->target, request->kind,
                                  request->capability, request->owner,
                                  request->object, args);

    for (;;) {
        outcome = npk_core_operation_outcome(slot);
        if (outcome != NPK_OUTCOME_PENDING) {
            break;
        }
        status = npk_core_advance(slot);
        if (status != 0) {
            ops->last_status = NPK_REQUEST_ADVANCE_FAIL;
            break;
        }
    }

    reason = npk_core_operation_reason(slot);
    response->result = npk_core_operation_result(slot);
    response->receipt_index = npk_core_operation_receipt_index(slot);
    response->reason = reason;
    response->error = npk_core_operation_device_error(slot);
    response->aux = npk_core_operation_device_evidence(slot);
    response->slot = slot;
    ops->last_reason = reason;
    ops->last_outcome = outcome;
    ops->last_receipt = response->receipt_index;

    if (ops->last_status == NPK_REQUEST_OK && status != 0) {
        ops->last_status = NPK_REQUEST_ADVANCE_FAIL;
    }
    if (ops->last_status == NPK_REQUEST_OK) {
        if (outcome == NPK_OUTCOME_REJECTED) {
            ops->last_status = NPK_REQUEST_REJECTED;
        } else if (outcome == NPK_OUTCOME_FAILED) {
            ops->last_status = NPK_REQUEST_FAILED;
        } else if (outcome != NPK_OUTCOME_COMPLETE) {
            ops->last_status = NPK_REQUEST_BAD_OUTCOME;
        }
    }
    if (outcome == NPK_OUTCOME_COMPLETE) {
        if (type <= NPK_OP_DEVICE_WRITE) {
            ops->device_operations = ops->device_operations + 1;
        } else {
            ops->timer_operations = ops->timer_operations + 1;
        }
    }
    response->status = ops->last_status;
    if (outcome == NPK_OUTCOME_COMPLETE ||
            outcome == NPK_OUTCOME_REJECTED ||
            outcome == NPK_OUTCOME_FAILED) {
        npk_core_release(slot);
    }
    return (int)ops->last_status;
}
