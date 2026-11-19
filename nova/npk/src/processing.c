/*
 * Minimal NPK processing pipeline for the boot foundation milestone.
 *
 * This is the first concrete implementation of the Processed Kernel model.
 * A typed boot-verification operation is admitted, validated, executed,
 * committed, and completed by one fixed processing unit. The receipt in low
 * memory makes every stage observable to the host test harness.
 *
 * No scheduler, allocator, trap producer, syscall producer, or device producer
 * exists yet. This file does not pretend otherwise.
 */

#define NPK_BOOT_MARKER_ADDRESS 0x90000
#define NPK_RECEIPT_ADDRESS 0x90008

#define NPK_PROCESS_MAGIC 0x4E504B50
#define NPK_OP_BOOT_VERIFY 1
#define NPK_BOOT_INPUT 0x4E504B31
#define NPK_PROCESSED_BOOT_OUTPUT 0x4E504B32

#define NPK_STAGE_ADMIT 1
#define NPK_STAGE_VALIDATE 2
#define NPK_STAGE_EXECUTE 3
#define NPK_STAGE_COMMIT 4
#define NPK_STAGE_COMPLETE 5
#define NPK_STAGE_COUNT 5

#define NPK_STATUS_PENDING 0
#define NPK_STATUS_PROCESSED 1
#define NPK_STATUS_REJECTED 2
#define NPK_STATUS_FAILED 3

typedef unsigned long npk_u64;

struct NpkOperationReceipt {
    npk_u64 magic;
    npk_u64 operation;
    npk_u64 stage;
    npk_u64 status;
    npk_u64 input;
    npk_u64 output;
    npk_u64 trace[NPK_STAGE_COUNT];
};

struct NpkProcessingUnit {
    npk_u64 id;
    npk_u64 stage;
    npk_u64 operation;
};

static struct NpkProcessingUnit pu0;

static struct NpkOperationReceipt *npk_receipt(void)
{
    return (struct NpkOperationReceipt *)NPK_RECEIPT_ADDRESS;
}

static void npk_record_stage(struct NpkProcessingUnit *unit,
                             struct NpkOperationReceipt *receipt,
                             npk_u64 stage)
{
    unit->stage = stage;
    receipt->stage = stage;
    receipt->trace[stage - 1] = stage;
}

static void npk_receipt_init(struct NpkOperationReceipt *receipt)
{
    npk_u64 i;

    receipt->magic = NPK_PROCESS_MAGIC;
    receipt->operation = NPK_OP_BOOT_VERIFY;
    receipt->stage = 0;
    receipt->status = NPK_STATUS_PENDING;
    receipt->input = NPK_BOOT_INPUT;
    receipt->output = 0;
    for (i = 0; i < NPK_STAGE_COUNT; i = i + 1) {
        receipt->trace[i] = 0;
    }
}

static int npk_stage_admit(struct NpkProcessingUnit *unit,
                           struct NpkOperationReceipt *receipt)
{
    if (unit->stage != 0) {
        return 11;
    }
    if (receipt->operation != NPK_OP_BOOT_VERIFY) {
        return 12;
    }
    if (receipt->input != NPK_BOOT_INPUT) {
        return 13;
    }
    if (receipt->status != NPK_STATUS_PENDING) {
        return 14;
    }
    npk_record_stage(unit, receipt, NPK_STAGE_ADMIT);
    return 0;
}

static int npk_stage_validate(struct NpkProcessingUnit *unit,
                              struct NpkOperationReceipt *receipt)
{
    if (unit->stage != NPK_STAGE_ADMIT) {
        return 21;
    }
    if (receipt->magic != NPK_PROCESS_MAGIC) {
        return 22;
    }
    if (receipt->operation != NPK_OP_BOOT_VERIFY) {
        return 23;
    }
    if (receipt->input != NPK_BOOT_INPUT) {
        return 24;
    }
    if (receipt->status != NPK_STATUS_PENDING) {
        return 25;
    }
    if (receipt->output != 0) {
        return 26;
    }
    npk_record_stage(unit, receipt, NPK_STAGE_VALIDATE);
    return 0;
}

static int npk_stage_execute(struct NpkProcessingUnit *unit,
                             struct NpkOperationReceipt *receipt)
{
    npk_u64 observed_marker;

    if (unit->stage != NPK_STAGE_VALIDATE) {
        return 31;
    }
    observed_marker = *((npk_u64 *)NPK_BOOT_MARKER_ADDRESS);
    if (observed_marker != receipt->input) {
        return 32;
    }
    receipt->output = NPK_PROCESSED_BOOT_OUTPUT;
    npk_record_stage(unit, receipt, NPK_STAGE_EXECUTE);
    return 0;
}

static int npk_stage_commit(struct NpkProcessingUnit *unit,
                            struct NpkOperationReceipt *receipt)
{
    if (unit->stage != NPK_STAGE_EXECUTE) {
        return 41;
    }
    if (receipt->output != NPK_PROCESSED_BOOT_OUTPUT) {
        return 42;
    }
    if (receipt->status != NPK_STATUS_PENDING) {
        return 43;
    }
    receipt->status = NPK_STATUS_PROCESSED;
    npk_record_stage(unit, receipt, NPK_STAGE_COMMIT);
    return 0;
}

static int npk_stage_complete(struct NpkProcessingUnit *unit,
                              struct NpkOperationReceipt *receipt)
{
    if (unit->stage != NPK_STAGE_COMMIT) {
        return 51;
    }
    if (receipt->status != NPK_STATUS_PROCESSED) {
        return 52;
    }
    if (receipt->output != NPK_PROCESSED_BOOT_OUTPUT) {
        return 53;
    }
    npk_record_stage(unit, receipt, NPK_STAGE_COMPLETE);
    return 0;
}

static int npk_pu_advance(struct NpkProcessingUnit *unit,
                          struct NpkOperationReceipt *receipt)
{
    if (unit->stage == 0) {
        return npk_stage_admit(unit, receipt);
    }
    if (unit->stage == NPK_STAGE_ADMIT) {
        return npk_stage_validate(unit, receipt);
    }
    if (unit->stage == NPK_STAGE_VALIDATE) {
        return npk_stage_execute(unit, receipt);
    }
    if (unit->stage == NPK_STAGE_EXECUTE) {
        return npk_stage_commit(unit, receipt);
    }
    if (unit->stage == NPK_STAGE_COMMIT) {
        return npk_stage_complete(unit, receipt);
    }
    return 61;
}

int npk_process_boot(void)
{
    struct NpkOperationReceipt *receipt = npk_receipt();
    npk_u64 i;
    int step;
    int result;

    npk_receipt_init(receipt);
    pu0.id = 0;
    pu0.stage = 0;
    pu0.operation = NPK_OP_BOOT_VERIFY;

    for (step = 0; step < NPK_STAGE_COUNT; step = step + 1) {
        result = npk_pu_advance(&pu0, receipt);
        if (result != 0) {
            receipt->status = NPK_STATUS_FAILED;
            return result;
        }
    }

    for (i = 0; i < NPK_STAGE_COUNT; i = i + 1) {
        if (receipt->trace[i] != i + 1) {
            receipt->status = NPK_STATUS_FAILED;
            return 70;
        }
    }
    if (receipt->stage != NPK_STAGE_COMPLETE) {
        receipt->status = NPK_STATUS_FAILED;
        return 71;
    }
    if (receipt->status != NPK_STATUS_PROCESSED) {
        return 72;
    }
    if (receipt->output != NPK_PROCESSED_BOOT_OUTPUT) {
        receipt->status = NPK_STATUS_FAILED;
        return 73;
    }
    return 0;
}
