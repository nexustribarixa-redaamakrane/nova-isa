/*
 * NPK reusable processing core.
 *
 * This subsystem owns typed operation records, processing-unit ownership, and
 * the controlled ADMIT -> VALIDATE -> EXECUTE -> COMMIT -> COMPLETE lifecycle.
 * The Milestone 1 boot receipt at 0x90008 is preserved byte-for-byte and is
 * synchronized from the generic boot operation. A larger receipt log makes the
 * Milestone 2 core observable to host tests.
 *
 * The two processing units are modeled sequentially. NovaMachine has no kernel
 * SMP support in Phase 2, so this is not a claim of hardware parallelism.
 */

#include "npk_core.h"

#define NPK_BOOT_MARKER_ADDRESS 0x90000
#define NPK_LEGACY_RECEIPT_ADDRESS 0x90008
#define NPK_CORE_STATE_ADDRESS 0x90080
#define NPK_OP_SNAPSHOT_ADDRESS 0x90120
#define NPK_RECEIPT_TABLE_ADDRESS 0x90220
#define NPK_TRAP_RECEIPT_TABLE_ADDRESS 0x90B00
#define NPK_MEMORY_RECEIPT_TABLE_ADDRESS 0x92050

#define NPK_PROCESS_MAGIC 0x4E504B50
#define NPK_CORE_MAGIC 0x4E504B43
#define NPK_CORE_VERSION 2
#define NPK_RECEIPT_MAGIC 0x4E504B52

#define NPK_OP_SLOTS 4
#define NPK_PU_COUNT 2
#define NPK_RECEIPT_SLOTS 12
#define NPK_TRAP_RECEIPT_SLOTS 8
#define NPK_MEMORY_RECEIPT_SLOTS 128
#define NPK_STAGE_COUNT 5

#define NPK_OP_BOOT_VERIFY 1
#define NPK_OP_CORE_DIAGNOSTIC 2
#define NPK_OP_TRAP_PROCESS 3
#define NPK_OP_MEMORY_ALLOCATE 4
#define NPK_OP_MEMORY_RELEASE 5
#define NPK_OP_MEMORY_MAP 6
#define NPK_OP_MEMORY_UNMAP 7
#define NPK_OP_MEMORY_QUERY 8

#define NPK_BOOT_INPUT 0x4E504B31
#define NPK_PROCESSED_BOOT_OUTPUT 0x4E504B32
#define NPK_DIAG_OUTPUT 0x4E504B44

#define NPK_DIAG_SUCCESS 0
#define NPK_DIAG_REJECT 1
#define NPK_DIAG_EXEC_FAIL 2
#define NPK_DIAG_COMMIT_FAIL 3

#define NPK_STAGE_NONE 0
#define NPK_STAGE_ADMIT 1
#define NPK_STAGE_VALIDATE 2
#define NPK_STAGE_EXECUTE 3
#define NPK_STAGE_COMMIT 4
#define NPK_STAGE_COMPLETE 5

#define NPK_OP_STATE_FREE 0
#define NPK_OP_STATE_ADMITTED 1
#define NPK_OP_STATE_VALIDATED 2
#define NPK_OP_STATE_EXECUTED 3
#define NPK_OP_STATE_COMMITTED 4
#define NPK_OP_STATE_COMPLETE 5
#define NPK_OP_STATE_REJECTED 6
#define NPK_OP_STATE_FAILED 7

#define NPK_OUTCOME_NONE 0
#define NPK_OUTCOME_PENDING 1
#define NPK_OUTCOME_COMPLETE 2
#define NPK_OUTCOME_REJECTED 3
#define NPK_OUTCOME_FAILED 4

#define NPK_PU_IDLE 0
#define NPK_PU_BUSY 1

#define NPK_LEGACY_PENDING 0
#define NPK_LEGACY_PROCESSED 1
#define NPK_LEGACY_REJECTED 2
#define NPK_LEGACY_FAILED 3

#define NPK_REASON_NONE 0
#define NPK_REASON_BAD_ID 1
#define NPK_REASON_BAD_TYPE 2
#define NPK_REASON_DUPLICATE_ID 3
#define NPK_REASON_NO_SLOT 4
#define NPK_REASON_UNIT_UNAVAILABLE 5
#define NPK_REASON_NO_RECEIPT 6
#define NPK_REASON_BAD_INPUT 7
#define NPK_REASON_BOOT_MARKER_MISMATCH 8
#define NPK_REASON_DIAG_REJECT 9
#define NPK_REASON_DIAG_EXEC 10
#define NPK_REASON_DIAG_COMMIT 11
#define NPK_REASON_CONTRACT 12
#define NPK_REASON_BAD_OUTPUT 13
#define NPK_REASON_TRAP_BAD_FRAME 14
#define NPK_REASON_TRAP_BAD_ECODE 15
#define NPK_REASON_TRAP_UNSUPPORTED 16
#define NPK_REASON_TRAP_STATE 17
#define NPK_REASON_MEMORY_BAD_REQUEST 18
#define NPK_REASON_MEMORY_BAD_REGION 19
#define NPK_REASON_MEMORY_BAD_SIZE 20
#define NPK_REASON_MEMORY_BAD_ALIGN 21
#define NPK_REASON_MEMORY_OVERLAP 22
#define NPK_REASON_MEMORY_EXHAUSTED 23
#define NPK_REASON_MEMORY_OWNER 24
#define NPK_REASON_MEMORY_STATE 25
#define NPK_REASON_MEMORY_BAD_MAPPING 26
#define NPK_REASON_MEMORY_UNSUPPORTED 27
#define NPK_REASON_MEMORY_BAD_ADDRESS_SPACE 28
#define NPK_REASON_MEMORY_PTE_CONFLICT 29

#define NPK_OK 0
#define NPK_ERR_BAD_ID 1
#define NPK_ERR_BAD_TYPE 2
#define NPK_ERR_DUPLICATE_ID 3
#define NPK_ERR_NO_SLOT 4
#define NPK_ERR_UNIT_UNAVAILABLE 5
#define NPK_ERR_NO_RECEIPT 6
#define NPK_ERR_BAD_SLOT 7
#define NPK_ERR_NOT_ACTIVE 8
#define NPK_ERR_OWNERSHIP 9
#define NPK_ERR_INVALID_TRANSITION 10
#define NPK_ERR_UNIT_BUSY 11
#define NPK_ERR_UNIT_IDLE 12
#define NPK_ERR_BAD_PU 13
#define NPK_ERR_INCOMPLETE 14

struct NpkLegacyReceipt {
    npk_u64 magic;
    npk_u64 operation;
    npk_u64 stage;
    npk_u64 status;
    npk_u64 input;
    npk_u64 output;
    npk_u64 trace[NPK_STAGE_COUNT];
};

struct NpkReceipt {
    npk_u64 magic;
    npk_u64 id;
    npk_u64 type;
    npk_u64 pu;
    npk_u64 stage;
    npk_u64 state;
    npk_u64 outcome;
    npk_u64 result;
    npk_u64 reason;
    npk_u64 trace[NPK_STAGE_COUNT];
    npk_u64 reserved[2];
};

struct NpkOperationSnapshot {
    npk_u64 id;
    npk_u64 type;
    npk_u64 stage;
    npk_u64 state;
    npk_u64 pu;
    npk_u64 result;
    npk_u64 reason;
    npk_u64 reserved;
};

struct NpkCoreStateSnapshot {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 op_slots;
    npk_u64 pu_count;
    npk_u64 admitted;
    npk_u64 rejected;
    npk_u64 failed;
    npk_u64 completed;
    npk_u64 last_admit_status;
    npk_u64 last_admit_reason;
    npk_u64 last_pu_status;
    npk_u64 last_transition_status;
    npk_u64 selftest_status;
    npk_u64 reserved;
    npk_u64 pu_state[NPK_PU_COUNT];
    npk_u64 pu_operation[NPK_PU_COUNT];
};

struct NpkProcessingUnit {
    npk_u64 id;
    npk_u64 state;
    npk_u64 operation_id;
    npk_u64 operation_slot;
};

static struct NpkOperation operations[NPK_OP_SLOTS];
static struct NpkProcessingUnit processing_units[NPK_PU_COUNT];
static npk_u64 next_receipt_index;
static npk_u64 next_trap_receipt_index;
static npk_u64 next_memory_receipt_index;

static struct NpkLegacyReceipt *npk_legacy_receipt(void)
{
    return (struct NpkLegacyReceipt *)NPK_LEGACY_RECEIPT_ADDRESS;
}

static struct NpkCoreStateSnapshot *npk_core_state(void)
{
    return (struct NpkCoreStateSnapshot *)NPK_CORE_STATE_ADDRESS;
}

static struct NpkOperationSnapshot *npk_op_snapshot(npk_u64 slot)
{
    return (struct NpkOperationSnapshot *)(NPK_OP_SNAPSHOT_ADDRESS +
        slot * sizeof(struct NpkOperationSnapshot));
}

static npk_u64 npk_boot_validate(struct NpkOperation *op);
static npk_u64 npk_boot_execute(struct NpkOperation *op);
static npk_u64 npk_boot_commit(struct NpkOperation *op);
static npk_u64 npk_diag_validate(struct NpkOperation *op);
static npk_u64 npk_diag_execute(struct NpkOperation *op);
static npk_u64 npk_diag_commit(struct NpkOperation *op);
static npk_u64 npk_trap_validate(struct NpkOperation *op);
static npk_u64 npk_trap_execute(struct NpkOperation *op);
static npk_u64 npk_trap_commit(struct NpkOperation *op);
static npk_u64 npk_memory_validate(struct NpkOperation *op);
static npk_u64 npk_memory_execute(struct NpkOperation *op);
static npk_u64 npk_memory_commit(struct NpkOperation *op);
static void npk_core_publish(struct NpkOperation *op);

static struct NpkReceipt *npk_receipt_at(npk_u64 base, npk_u64 index)
{
    return (struct NpkReceipt *)(base + index * sizeof(struct NpkReceipt));
}

static struct NpkReceipt *npk_receipt_for(struct NpkOperation *op)
{
    return npk_receipt_at(op->receipt_base, op->receipt_index);
}

static int npk_core_is_memory_type(npk_u64 type)
{
    if (type == NPK_OP_MEMORY_ALLOCATE ||
            type == NPK_OP_MEMORY_RELEASE ||
            type == NPK_OP_MEMORY_MAP ||
            type == NPK_OP_MEMORY_UNMAP ||
            type == NPK_OP_MEMORY_QUERY) {
        return 1;
    }
    return 0;
}

static npk_u64 npk_core_receipt_base(npk_u64 type)
{
    if (type == NPK_OP_TRAP_PROCESS) {
        return NPK_TRAP_RECEIPT_TABLE_ADDRESS;
    }
    if (npk_core_is_memory_type(type)) {
        return NPK_MEMORY_RECEIPT_TABLE_ADDRESS;
    }
    return NPK_RECEIPT_TABLE_ADDRESS;
}

static npk_u64 npk_core_receipt_slots(npk_u64 type)
{
    if (type == NPK_OP_TRAP_PROCESS) {
        return NPK_TRAP_RECEIPT_SLOTS;
    }
    if (npk_core_is_memory_type(type)) {
        return NPK_MEMORY_RECEIPT_SLOTS;
    }
    return NPK_RECEIPT_SLOTS;
}

static npk_u64 *npk_core_next_receipt(npk_u64 type)
{
    if (type == NPK_OP_TRAP_PROCESS) {
        return &next_trap_receipt_index;
    }
    if (npk_core_is_memory_type(type)) {
        return &next_memory_receipt_index;
    }
    return &next_receipt_index;
}

static int npk_core_valid_type(npk_u64 type)
{
    if (type == NPK_OP_BOOT_VERIFY) {
        return 1;
    }
    if (type == NPK_OP_CORE_DIAGNOSTIC) {
        return 1;
    }
    if (type == NPK_OP_TRAP_PROCESS) {
        return 1;
    }
    if (npk_core_is_memory_type(type)) {
        return 1;
    }
    return 0;
}

static void npk_receipt_clear(struct NpkReceipt *receipt)
{
    npk_u64 i;

    receipt->magic = 0;
    receipt->id = 0;
    receipt->type = 0;
    receipt->pu = NPK_NO_INDEX;
    receipt->stage = 0;
    receipt->state = 0;
    receipt->outcome = 0;
    receipt->result = 0;
    receipt->reason = 0;
    for (i = 0; i < NPK_STAGE_COUNT; i = i + 1) {
        receipt->trace[i] = 0;
    }
    for (i = 0; i < 2; i = i + 1) {
        receipt->reserved[i] = 0;
    }
}

static void npk_core_publish_state(void)
{
    struct NpkCoreStateSnapshot *state = npk_core_state();
    npk_u64 i;

    state->magic = NPK_CORE_MAGIC;
    state->version = NPK_CORE_VERSION;
    state->op_slots = NPK_OP_SLOTS;
    state->pu_count = NPK_PU_COUNT;
    for (i = 0; i < NPK_PU_COUNT; i = i + 1) {
        state->pu_state[i] = processing_units[i].state;
        state->pu_operation[i] = processing_units[i].operation_id;
    }
}

static void npk_core_publish_op(struct NpkOperation *op)
{
    struct NpkOperationSnapshot *snapshot;
    npk_u64 slot;

    if (op == 0) {
        for (slot = 0; slot < NPK_OP_SLOTS; slot = slot + 1) {
            npk_core_publish_op(&operations[slot]);
        }
        return;
    }

    slot = (npk_u64)(op - operations);
    snapshot = npk_op_snapshot(slot);
    snapshot->id = op->id;
    snapshot->type = op->type;
    snapshot->stage = op->stage;
    snapshot->state = op->state;
    snapshot->pu = op->pu;
    snapshot->result = op->result;
    snapshot->reason = op->reason;
    snapshot->reserved = 0;
}

static void npk_core_sync_receipt(struct NpkOperation *op)
{
    struct NpkReceipt *receipt;

    if (op->receipt_index >= npk_core_receipt_slots(op->type)) {
        return;
    }
    receipt = npk_receipt_for(op);
    receipt->stage = op->stage;
    receipt->state = op->state;
    receipt->outcome = op->outcome;
    receipt->result = op->result;
    receipt->reason = op->reason;
    if (npk_core_is_memory_type(op->type)) {
        receipt->reserved[0] = op->memory_size;
        receipt->reserved[1] = op->memory_evidence;
    }
}

static void npk_core_sync_legacy(struct NpkOperation *op)
{
    struct NpkLegacyReceipt *legacy = npk_legacy_receipt();
    struct NpkReceipt *receipt;
    npk_u64 i;

    if (op->type != NPK_OP_BOOT_VERIFY ||
            op->receipt_index >= NPK_RECEIPT_SLOTS) {
        return;
    }
    receipt = npk_receipt_for(op);

    legacy->magic = NPK_PROCESS_MAGIC;
    legacy->operation = op->type;
    legacy->stage = op->stage;
    if (op->outcome == NPK_OUTCOME_COMPLETE) {
        legacy->status = NPK_LEGACY_PROCESSED;
    } else if (op->outcome == NPK_OUTCOME_REJECTED) {
        legacy->status = NPK_LEGACY_REJECTED;
    } else if (op->outcome == NPK_OUTCOME_FAILED) {
        legacy->status = NPK_LEGACY_FAILED;
    } else {
        legacy->status = NPK_LEGACY_PENDING;
    }
    legacy->input = op->input;
    legacy->output = op->result;
    for (i = 0; i < NPK_STAGE_COUNT; i = i + 1) {
        legacy->trace[i] = receipt->trace[i];
    }
}

static void npk_core_publish(struct NpkOperation *op)
{
    npk_core_sync_receipt(op);
    npk_core_publish_op(op);
    npk_core_publish_state();
    npk_core_sync_legacy(op);
}

static void npk_core_mark_stage(struct NpkOperation *op, npk_u64 stage)
{
    struct NpkReceipt *receipt;

    op->stage = stage;
    if (op->receipt_index >= npk_core_receipt_slots(op->type)) {
        return;
    }
    receipt = npk_receipt_for(op);
    if (stage >= NPK_STAGE_ADMIT && stage <= NPK_STAGE_COMPLETE) {
        receipt->trace[stage - 1] = stage;
    }
}

static void npk_core_init_receipt(struct NpkOperation *op)
{
    struct NpkReceipt *receipt;

    if (op->receipt_index >= npk_core_receipt_slots(op->type)) {
        return;
    }
    receipt = npk_receipt_for(op);
    npk_receipt_clear(receipt);
    receipt->magic = NPK_RECEIPT_MAGIC;
    receipt->id = op->id;
    receipt->type = op->type;
    receipt->pu = op->pu;
    receipt->stage = NPK_STAGE_ADMIT;
    receipt->state = NPK_OP_STATE_ADMITTED;
    receipt->outcome = NPK_OUTCOME_PENDING;
    receipt->trace[0] = NPK_STAGE_ADMIT;
}

void npk_core_init(void)
{
    struct NpkCoreStateSnapshot *state = npk_core_state();
    npk_u64 i;

    next_receipt_index = 0;
    next_trap_receipt_index = 0;
    next_memory_receipt_index = 0;

    for (i = 0; i < NPK_OP_SLOTS; i = i + 1) {
        operations[i].id = 0;
        operations[i].type = 0;
        operations[i].stage = 0;
        operations[i].state = NPK_OP_STATE_FREE;
        operations[i].pu = NPK_NO_INDEX;
        operations[i].input = 0;
        operations[i].provisional = 0;
        operations[i].result = 0;
        operations[i].reason = 0;
        operations[i].outcome = NPK_OUTCOME_NONE;
        operations[i].receipt_index = NPK_NO_INDEX;
        operations[i].receipt_base = NPK_RECEIPT_TABLE_ADDRESS;
        operations[i].memory_address = 0;
        operations[i].memory_size = 0;
        operations[i].memory_flags = 0;
        operations[i].memory_aux = 0;
        operations[i].memory_evidence = 0;
        operations[i].memory_scratch = 0;
    }
    for (i = 0; i < NPK_PU_COUNT; i = i + 1) {
        processing_units[i].id = i;
        processing_units[i].state = NPK_PU_IDLE;
        processing_units[i].operation_id = 0;
        processing_units[i].operation_slot = NPK_NO_INDEX;
    }
    for (i = 0; i < NPK_RECEIPT_SLOTS; i = i + 1) {
        npk_receipt_clear(npk_receipt_at(NPK_RECEIPT_TABLE_ADDRESS, i));
    }
    for (i = 0; i < NPK_TRAP_RECEIPT_SLOTS; i = i + 1) {
        npk_receipt_clear(npk_receipt_at(NPK_TRAP_RECEIPT_TABLE_ADDRESS, i));
    }
    /* Memory receipts are zero-filled by sparse memory and each used slot is
     * cleared by npk_core_init_receipt before it is published. */

    state->admitted = 0;
    state->rejected = 0;
    state->failed = 0;
    state->completed = 0;
    state->last_admit_status = NPK_OK;
    state->last_admit_reason = NPK_REASON_NONE;
    state->last_pu_status = NPK_OK;
    state->last_transition_status = NPK_OK;
    state->selftest_status = NPK_ERR_INVALID_TRANSITION;
    state->reserved = 0;

    for (i = 0; i < NPK_OP_SLOTS; i = i + 1) {
        npk_core_publish_op(&operations[i]);
    }
    npk_core_publish_state();
}

static npk_u64 npk_reason_for_status(int status)
{
    if (status == NPK_ERR_BAD_ID) {
        return NPK_REASON_BAD_ID;
    }
    if (status == NPK_ERR_BAD_TYPE) {
        return NPK_REASON_BAD_TYPE;
    }
    if (status == NPK_ERR_DUPLICATE_ID) {
        return NPK_REASON_DUPLICATE_ID;
    }
    if (status == NPK_ERR_NO_SLOT) {
        return NPK_REASON_NO_SLOT;
    }
    if (status == NPK_ERR_UNIT_UNAVAILABLE) {
        return NPK_REASON_UNIT_UNAVAILABLE;
    }
    if (status == NPK_ERR_NO_RECEIPT) {
        return NPK_REASON_NO_RECEIPT;
    }
    return NPK_REASON_CONTRACT;
}

static void npk_core_record_admission_receipt(npk_u64 id, npk_u64 type,
                                              npk_u64 reason)
{
    struct NpkReceipt *receipt;
    struct NpkCoreStateSnapshot *core = npk_core_state();
    npk_u64 *next = npk_core_next_receipt(type);

    if (*next >= npk_core_receipt_slots(type)) {
        core->rejected = core->rejected + 1;
        return;
    }
    receipt = npk_receipt_at(npk_core_receipt_base(type), *next);
    npk_receipt_clear(receipt);
    receipt->magic = NPK_RECEIPT_MAGIC;
    receipt->id = id;
    receipt->type = type;
    receipt->pu = NPK_NO_INDEX;
    receipt->stage = NPK_STAGE_NONE;
    receipt->state = NPK_OP_STATE_REJECTED;
    receipt->outcome = NPK_OUTCOME_REJECTED;
    receipt->reason = reason;
    *next = *next + 1;
    core->rejected = core->rejected + 1;
}

static int npk_core_admission_fail(npk_u64 id, npk_u64 type, int status)
{
    npk_u64 reason = npk_reason_for_status(status);
    struct NpkCoreStateSnapshot *core = npk_core_state();

    core->last_admit_status = (npk_u64)status;
    core->last_admit_reason = reason;
    npk_core_record_admission_receipt(id, type, reason);
    npk_core_publish_state();
    return status;
}

static npk_u64 npk_core_find_existing(npk_u64 id)
{
    npk_u64 i;

    for (i = 0; i < NPK_OP_SLOTS; i = i + 1) {
        if (operations[i].state != NPK_OP_STATE_FREE && operations[i].id == id) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_core_find_free_slot(void)
{
    npk_u64 i;

    for (i = 0; i < NPK_OP_SLOTS; i = i + 1) {
        if (operations[i].state == NPK_OP_STATE_FREE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_core_find_idle_pu(void)
{
    npk_u64 i;

    for (i = 0; i < NPK_PU_COUNT; i = i + 1) {
        if (processing_units[i].state == NPK_PU_IDLE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

int npk_core_admit(npk_u64 type, npk_u64 id, npk_u64 input, npk_u64 *out_slot)
{
    npk_u64 slot;
    npk_u64 pu;
    npk_u64 base;
    npk_u64 *next;
    struct NpkOperation *op;
    struct NpkCoreStateSnapshot *core = npk_core_state();

    core->last_admit_status = NPK_OK;
    core->last_admit_reason = NPK_REASON_NONE;

    if (id == 0) {
        return npk_core_admission_fail(id, type, NPK_ERR_BAD_ID);
    }
    if (npk_core_valid_type(type) == 0) {
        return npk_core_admission_fail(id, type, NPK_ERR_BAD_TYPE);
    }
    if (npk_core_find_existing(id) != NPK_NO_INDEX) {
        return npk_core_admission_fail(id, type, NPK_ERR_DUPLICATE_ID);
    }
    slot = npk_core_find_free_slot();
    if (slot == NPK_NO_INDEX) {
        return npk_core_admission_fail(id, type, NPK_ERR_NO_SLOT);
    }
    pu = npk_core_find_idle_pu();
    if (pu == NPK_NO_INDEX) {
        return npk_core_admission_fail(id, type, NPK_ERR_UNIT_UNAVAILABLE);
    }
    base = npk_core_receipt_base(type);
    next = npk_core_next_receipt(type);
    if (*next >= npk_core_receipt_slots(type)) {
        return npk_core_admission_fail(id, type, NPK_ERR_NO_RECEIPT);
    }

    op = &operations[slot];
    op->id = id;
    op->type = type;
    op->stage = NPK_STAGE_ADMIT;
    op->state = NPK_OP_STATE_ADMITTED;
    op->pu = pu;
    op->input = input;
    op->provisional = 0;
    op->result = 0;
    op->reason = NPK_REASON_NONE;
    op->outcome = NPK_OUTCOME_PENDING;
    op->receipt_index = *next;
    op->receipt_base = base;
    op->memory_address = 0;
    op->memory_size = 0;
    op->memory_flags = 0;
    op->memory_aux = 0;
    op->memory_evidence = 0;
    op->memory_scratch = 0;
    *next = *next + 1;

    processing_units[pu].state = NPK_PU_BUSY;
    processing_units[pu].operation_id = id;
    processing_units[pu].operation_slot = slot;

    npk_core_init_receipt(op);
    core->admitted = core->admitted + 1;
    npk_core_publish(op);
    *out_slot = slot;
    return NPK_OK;
}

void npk_core_set_memory_arguments(npk_u64 slot, npk_u64 address,
                                   npk_u64 size, npk_u64 flags, npk_u64 aux)
{
    struct NpkOperation *op;

    if (slot >= NPK_OP_SLOTS) {
        return;
    }
    op = &operations[slot];
    op->memory_address = address;
    op->memory_size = size;
    op->memory_flags = flags;
    op->memory_aux = aux;
    op->memory_evidence = 0;
    op->memory_scratch = 0;
}

static int npk_core_is_terminal(npk_u64 state)
{
    if (state == NPK_OP_STATE_COMPLETE) {
        return 1;
    }
    if (state == NPK_OP_STATE_REJECTED) {
        return 1;
    }
    if (state == NPK_OP_STATE_FAILED) {
        return 1;
    }
    return 0;
}

static void npk_core_terminate(struct NpkOperation *op, npk_u64 state,
                               npk_u64 outcome, npk_u64 reason)
{
    struct NpkCoreStateSnapshot *core = npk_core_state();

    op->state = state;
    op->outcome = outcome;
    op->reason = reason;
    if (outcome != NPK_OUTCOME_COMPLETE) {
        op->provisional = 0;
        op->result = 0;
    }
    if (outcome == NPK_OUTCOME_COMPLETE) {
        core->completed = core->completed + 1;
    } else if (outcome == NPK_OUTCOME_REJECTED) {
        core->rejected = core->rejected + 1;
    } else if (outcome == NPK_OUTCOME_FAILED) {
        core->failed = core->failed + 1;
    }
    npk_core_publish(op);
}

int npk_core_advance_as(npk_u64 slot, npk_u64 requested_pu)
{
    struct NpkOperation *op;
    struct NpkCoreStateSnapshot *core = npk_core_state();
    npk_u64 reason;

    if (slot >= NPK_OP_SLOTS) {
        core->last_transition_status = NPK_ERR_BAD_SLOT;
        return NPK_ERR_BAD_SLOT;
    }
    op = &operations[slot];
    if (op->state == NPK_OP_STATE_FREE) {
        core->last_transition_status = NPK_ERR_NOT_ACTIVE;
        return NPK_ERR_NOT_ACTIVE;
    }
    if (npk_core_is_terminal(op->state) == 1) {
        core->last_transition_status = NPK_ERR_INVALID_TRANSITION;
        return NPK_ERR_INVALID_TRANSITION;
    }
    if (op->pu != requested_pu) {
        core->last_transition_status = NPK_ERR_OWNERSHIP;
        return NPK_ERR_OWNERSHIP;
    }
    if (requested_pu >= NPK_PU_COUNT ||
            processing_units[requested_pu].operation_slot != slot) {
        core->last_transition_status = NPK_ERR_OWNERSHIP;
        return NPK_ERR_OWNERSHIP;
    }

    if (op->state == NPK_OP_STATE_ADMITTED) {
        npk_core_mark_stage(op, NPK_STAGE_VALIDATE);
        if (op->type == NPK_OP_BOOT_VERIFY) {
            reason = npk_boot_validate(op);
        } else if (op->type == NPK_OP_TRAP_PROCESS) {
            reason = npk_trap_validate(op);
        } else if (npk_core_is_memory_type(op->type)) {
            reason = npk_memory_validate(op);
        } else {
            reason = npk_diag_validate(op);
        }
        if (reason != NPK_REASON_NONE) {
            npk_core_terminate(op, NPK_OP_STATE_REJECTED,
                               NPK_OUTCOME_REJECTED, reason);
            return NPK_OK;
        }
        op->state = NPK_OP_STATE_VALIDATED;
        npk_core_publish(op);
        return NPK_OK;
    }

    if (op->state == NPK_OP_STATE_VALIDATED) {
        npk_core_mark_stage(op, NPK_STAGE_EXECUTE);
        if (op->type == NPK_OP_BOOT_VERIFY) {
            reason = npk_boot_execute(op);
        } else if (op->type == NPK_OP_TRAP_PROCESS) {
            reason = npk_trap_execute(op);
        } else if (npk_core_is_memory_type(op->type)) {
            reason = npk_memory_execute(op);
        } else {
            reason = npk_diag_execute(op);
        }
        if (reason != NPK_REASON_NONE) {
            npk_core_terminate(op, NPK_OP_STATE_FAILED, NPK_OUTCOME_FAILED,
                               reason);
            return NPK_OK;
        }
        op->state = NPK_OP_STATE_EXECUTED;
        npk_core_publish(op);
        return NPK_OK;
    }

    if (op->state == NPK_OP_STATE_EXECUTED) {
        npk_core_mark_stage(op, NPK_STAGE_COMMIT);
        if (op->type == NPK_OP_BOOT_VERIFY) {
            reason = npk_boot_commit(op);
        } else if (op->type == NPK_OP_TRAP_PROCESS) {
            reason = npk_trap_commit(op);
        } else if (npk_core_is_memory_type(op->type)) {
            reason = npk_memory_commit(op);
        } else {
            reason = npk_diag_commit(op);
        }
        if (reason != NPK_REASON_NONE) {
            npk_core_terminate(op, NPK_OP_STATE_FAILED, NPK_OUTCOME_FAILED,
                               reason);
            return NPK_OK;
        }
        op->state = NPK_OP_STATE_COMMITTED;
        npk_core_publish(op);
        return NPK_OK;
    }

    if (op->state == NPK_OP_STATE_COMMITTED) {
        npk_core_mark_stage(op, NPK_STAGE_COMPLETE);
        npk_core_terminate(op, NPK_OP_STATE_COMPLETE, NPK_OUTCOME_COMPLETE,
                           NPK_REASON_NONE);
        return NPK_OK;
    }

    core->last_transition_status = NPK_ERR_INVALID_TRANSITION;
    return NPK_ERR_INVALID_TRANSITION;
}

int npk_core_advance(npk_u64 slot)
{
    if (slot >= NPK_OP_SLOTS) {
        npk_core_state()->last_transition_status = NPK_ERR_BAD_SLOT;
        return NPK_ERR_BAD_SLOT;
    }
    return npk_core_advance_as(slot, operations[slot].pu);
}

int npk_core_release(npk_u64 slot)
{
    struct NpkOperation *op;
    struct NpkCoreStateSnapshot *core = npk_core_state();
    npk_u64 pu;

    if (slot >= NPK_OP_SLOTS) {
        return NPK_ERR_BAD_SLOT;
    }
    op = &operations[slot];
    if (op->state == NPK_OP_STATE_FREE) {
        return NPK_ERR_NOT_ACTIVE;
    }
    if (npk_core_is_terminal(op->state) == 0) {
        return NPK_ERR_INCOMPLETE;
    }
    pu = op->pu;
    if (pu >= NPK_PU_COUNT || processing_units[pu].operation_slot != slot) {
        return NPK_ERR_OWNERSHIP;
    }

    processing_units[pu].state = NPK_PU_IDLE;
    processing_units[pu].operation_id = 0;
    processing_units[pu].operation_slot = NPK_NO_INDEX;

    op->state = NPK_OP_STATE_FREE;
    op->stage = NPK_STAGE_NONE;
    op->pu = NPK_NO_INDEX;
    op->outcome = NPK_OUTCOME_NONE;
    op->id = 0;
    op->type = 0;
    op->input = 0;
    op->provisional = 0;
    op->result = 0;
    op->reason = NPK_REASON_NONE;
    op->receipt_index = NPK_NO_INDEX;
    op->receipt_base = NPK_RECEIPT_TABLE_ADDRESS;
    op->memory_address = 0;
    op->memory_size = 0;
    op->memory_flags = 0;
    op->memory_aux = 0;
    op->memory_evidence = 0;
    op->memory_scratch = 0;

    core->last_transition_status = NPK_OK;
    npk_core_publish_op(op);
    npk_core_publish_state();
    return NPK_OK;
}

int npk_core_operation_outcome(npk_u64 slot)
{
    if (slot >= NPK_OP_SLOTS) {
        return NPK_ERR_BAD_SLOT;
    }
    return (int)operations[slot].outcome;
}

npk_u64 npk_core_operation_result(npk_u64 slot)
{
    if (slot >= NPK_OP_SLOTS) {
        return 0;
    }
    return operations[slot].result;
}

npk_u64 npk_core_operation_reason(npk_u64 slot)
{
    if (slot >= NPK_OP_SLOTS) {
        return NPK_REASON_BAD_INPUT;
    }
    return operations[slot].reason;
}

npk_u64 npk_core_operation_receipt_index(npk_u64 slot)
{
    if (slot >= NPK_OP_SLOTS) {
        return NPK_NO_INDEX;
    }
    return operations[slot].receipt_index;
}

int npk_core_probe_busy_unit(npk_u64 pu)
{
    int status;
    struct NpkCoreStateSnapshot *core = npk_core_state();

    if (pu >= NPK_PU_COUNT) {
        status = NPK_ERR_BAD_PU;
    } else if (processing_units[pu].state == NPK_PU_BUSY) {
        status = NPK_ERR_UNIT_BUSY;
    } else {
        status = NPK_ERR_UNIT_IDLE;
    }
    core->last_pu_status = (npk_u64)status;
    npk_core_publish_state();
    return status;
}

int npk_core_probe_complete_uncommitted(npk_u64 slot)
{
    int status;
    struct NpkCoreStateSnapshot *core = npk_core_state();

    if (slot >= NPK_OP_SLOTS) {
        status = NPK_ERR_BAD_SLOT;
    } else if (operations[slot].state == NPK_OP_STATE_COMMITTED) {
        status = NPK_OK;
    } else {
        status = NPK_ERR_INVALID_TRANSITION;
    }
    core->last_transition_status = (npk_u64)status;
    npk_core_publish_state();
    return status;
}

static npk_u64 npk_boot_validate(struct NpkOperation *op)
{
    if (op->input != NPK_BOOT_INPUT) {
        return NPK_REASON_BAD_INPUT;
    }
    if (op->result != 0 || op->provisional != 0) {
        return NPK_REASON_CONTRACT;
    }
    return NPK_REASON_NONE;
}

static npk_u64 npk_boot_execute(struct NpkOperation *op)
{
    npk_u64 observed_marker = *((npk_u64 *)NPK_BOOT_MARKER_ADDRESS);

    if (observed_marker != op->input) {
        return NPK_REASON_BOOT_MARKER_MISMATCH;
    }
    op->provisional = NPK_PROCESSED_BOOT_OUTPUT;
    return NPK_REASON_NONE;
}

static npk_u64 npk_boot_commit(struct NpkOperation *op)
{
    if (op->provisional != NPK_PROCESSED_BOOT_OUTPUT) {
        return NPK_REASON_BAD_OUTPUT;
    }
    op->result = op->provisional;
    return NPK_REASON_NONE;
}

static npk_u64 npk_diag_validate(struct NpkOperation *op)
{
    if (op->input > NPK_DIAG_COMMIT_FAIL) {
        return NPK_REASON_BAD_INPUT;
    }
    if (op->input == NPK_DIAG_REJECT) {
        return NPK_REASON_DIAG_REJECT;
    }
    return NPK_REASON_NONE;
}

static npk_u64 npk_diag_execute(struct NpkOperation *op)
{
    if (op->input == NPK_DIAG_EXEC_FAIL) {
        return NPK_REASON_DIAG_EXEC;
    }
    op->provisional = NPK_DIAG_OUTPUT;
    return NPK_REASON_NONE;
}

static npk_u64 npk_diag_commit(struct NpkOperation *op)
{
    if (op->input == NPK_DIAG_COMMIT_FAIL) {
        return NPK_REASON_DIAG_COMMIT;
    }
    if (op->provisional != NPK_DIAG_OUTPUT) {
        return NPK_REASON_BAD_OUTPUT;
    }
    op->result = op->provisional;
    return NPK_REASON_NONE;
}

static void npk_expect_status(int actual, int expected, int *failures)
{
    if (actual != expected) {
        *failures = *failures + 1;
    }
}

static void npk_expect_u64(npk_u64 actual, npk_u64 expected, int *failures)
{
    if (actual != expected) {
        *failures = *failures + 1;
    }
}

static int npk_selftest_success(void)
{
    int failures = 0;
    int status;
    npk_u64 slot;

    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 2, NPK_DIAG_SUCCESS,
                            &slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_release(slot);
    npk_expect_status(status, NPK_ERR_INCOMPLETE, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_probe_complete_uncommitted(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    npk_expect_u64(operations[slot].outcome, NPK_OUTCOME_COMPLETE, &failures);
    npk_expect_u64(operations[slot].result, NPK_DIAG_OUTPUT, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_ERR_INVALID_TRANSITION, &failures);
    status = npk_core_release(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_probe_busy_unit(0);
    npk_expect_status(status, NPK_ERR_UNIT_IDLE, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_ERR_NOT_ACTIVE, &failures);
    return failures;
}

static int npk_selftest_reject(void)
{
    int failures = 0;
    int status;
    npk_u64 slot;

    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 3, NPK_DIAG_REJECT,
                            &slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    npk_expect_u64(operations[slot].outcome, NPK_OUTCOME_REJECTED, &failures);
    npk_expect_u64(operations[slot].stage, NPK_STAGE_VALIDATE, &failures);
    npk_expect_u64(operations[slot].result, 0, &failures);
    npk_expect_u64(operations[slot].reason, NPK_REASON_DIAG_REJECT, &failures);
    status = npk_core_release(slot);
    npk_expect_status(status, NPK_OK, &failures);
    return failures;
}

static int npk_selftest_exec_fail(void)
{
    int failures = 0;
    int status;
    npk_u64 slot;

    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 4, NPK_DIAG_EXEC_FAIL,
                            &slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    npk_expect_u64(operations[slot].outcome, NPK_OUTCOME_FAILED, &failures);
    npk_expect_u64(operations[slot].stage, NPK_STAGE_EXECUTE, &failures);
    npk_expect_u64(operations[slot].result, 0, &failures);
    npk_expect_u64(operations[slot].reason, NPK_REASON_DIAG_EXEC, &failures);
    status = npk_core_release(slot);
    npk_expect_status(status, NPK_OK, &failures);
    return failures;
}

static int npk_selftest_commit_fail(void)
{
    int failures = 0;
    int status;
    npk_u64 slot;

    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 5, NPK_DIAG_COMMIT_FAIL,
                            &slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    npk_expect_u64(operations[slot].outcome, NPK_OUTCOME_FAILED, &failures);
    npk_expect_u64(operations[slot].stage, NPK_STAGE_COMMIT, &failures);
    npk_expect_u64(operations[slot].result, 0, &failures);
    npk_expect_u64(operations[slot].reason, NPK_REASON_DIAG_COMMIT, &failures);
    status = npk_core_release(slot);
    npk_expect_status(status, NPK_OK, &failures);
    return failures;
}

static int npk_selftest_duplicate(void)
{
    int failures = 0;
    int status;
    npk_u64 slot;

    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 6, NPK_DIAG_SUCCESS,
                            &slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 6, NPK_DIAG_SUCCESS,
                            &slot);
    npk_expect_status(status, NPK_ERR_DUPLICATE_ID, &failures);
    status = npk_core_probe_busy_unit(0);
    npk_expect_status(status, NPK_ERR_UNIT_BUSY, &failures);
    status = npk_core_probe_busy_unit(2);
    npk_expect_status(status, NPK_ERR_BAD_PU, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_release(slot);
    npk_expect_status(status, NPK_OK, &failures);
    return failures;
}

static int npk_selftest_two_units(void)
{
    int failures = 0;
    int status;
    npk_u64 slot;
    npk_u64 first_slot;
    npk_u64 second_slot;

    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 7, NPK_DIAG_SUCCESS,
                            &first_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 8, NPK_DIAG_SUCCESS,
                            &second_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 9, NPK_DIAG_SUCCESS,
                            &slot);
    npk_expect_status(status, NPK_ERR_UNIT_UNAVAILABLE, &failures);

    status = npk_core_advance(first_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(first_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(first_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(first_slot);
    npk_expect_status(status, NPK_OK, &failures);

    status = npk_core_advance(second_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(second_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(second_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(second_slot);
    npk_expect_status(status, NPK_OK, &failures);

    status = npk_core_release(first_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_release(second_slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_probe_busy_unit(0);
    npk_expect_status(status, NPK_ERR_UNIT_IDLE, &failures);
    status = npk_core_probe_busy_unit(1);
    npk_expect_status(status, NPK_ERR_UNIT_IDLE, &failures);
    return failures;
}

static int npk_selftest_ownership(void)
{
    int failures = 0;
    int status;
    npk_u64 slot;

    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 10, NPK_DIAG_SUCCESS,
                            &slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance_as(slot, 1);
    npk_expect_status(status, NPK_ERR_OWNERSHIP, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_release(slot);
    npk_expect_status(status, NPK_OK, &failures);
    return failures;
}

static int npk_selftest_uncommitted(void)
{
    int failures = 0;
    int status;
    npk_u64 slot;

    status = npk_core_admit(NPK_OP_CORE_DIAGNOSTIC, 11, NPK_DIAG_SUCCESS,
                            &slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_probe_complete_uncommitted(slot);
    npk_expect_status(status, NPK_ERR_INVALID_TRANSITION, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_advance(slot);
    npk_expect_status(status, NPK_OK, &failures);
    status = npk_core_release(slot);
    npk_expect_status(status, NPK_OK, &failures);
    return failures;
}

int npk_core_selftest(void)
{
    int failures = 0;

    failures = failures + npk_selftest_success();
    failures = failures + npk_selftest_reject();
    failures = failures + npk_selftest_exec_fail();
    failures = failures + npk_selftest_commit_fail();
    failures = failures + npk_selftest_duplicate();
    failures = failures + npk_selftest_two_units();
    failures = failures + npk_selftest_ownership();
    failures = failures + npk_selftest_uncommitted();

    npk_core_state()->selftest_status = (npk_u64)failures;
    npk_core_publish_state();
    return failures;
}

int npk_process_boot(void)
{
    npk_u64 slot;
    int status;
    struct NpkOperation *op;

    npk_core_init();

    status = npk_core_admit(NPK_OP_BOOT_VERIFY, 1, NPK_BOOT_INPUT, &slot);
    if (status != NPK_OK) {
        return 10 + status;
    }
    status = npk_core_advance(slot);
    if (status != NPK_OK) {
        return 20 + status;
    }
    status = npk_core_advance(slot);
    if (status != NPK_OK) {
        return 21 + status;
    }
    status = npk_core_advance(slot);
    if (status != NPK_OK) {
        return 22 + status;
    }
    status = npk_core_advance(slot);
    if (status != NPK_OK) {
        return 23 + status;
    }

    op = &operations[slot];
    if (op->outcome != NPK_OUTCOME_COMPLETE) {
        return 40;
    }
    if (op->result != NPK_PROCESSED_BOOT_OUTPUT) {
        return 41;
    }
    if (op->pu != 0) {
        return 42;
    }
    status = npk_core_release(slot);
    if (status != NPK_OK) {
        return 50 + status;
    }

    status = npk_core_selftest();
    if (status != NPK_OK) {
        return 60 + status;
    }
    return NPK_OK;
}
