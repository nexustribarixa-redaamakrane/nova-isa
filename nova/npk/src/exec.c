/*
 * NPK Milestone 9 executable processing.
 *
 * This subsystem loads the toolchain's native flat .nob container, copies its
 * image into an existing M4 memory region wrapped by an M5 object, associates
 * the image with an M8 task, and enters user code through the ISA's documented
 * VMLAUNCH path. It has no ELF reader, no second object manager, no second
 * allocator, and no synthetic privilege transition. Every request is admitted
 * to the shared processing core and runs the real five-stage lifecycle.
 */

#include "npk_core.h"

#define NPK_EXEC_STATE_ADDRESS 0xB3010
#define NPK_EXEC_REQUEST_ADDRESS 0xB3090
#define NPK_EXEC_RESPONSE_ADDRESS 0xB30F0
#define NPK_EXEC_TABLE_ADDRESS 0xB3130
#define NPK_EXEC_LAUNCH_CONTEXT_ADDRESS 0xB4800
#define NPK_EXEC_SOURCE_ADDRESS 0xB5000

#define NPK_EXEC_STATE_MAGIC 0x4E504B45
#define NPK_EXEC_STATE_VERSION 1
#define NPK_REQUEST_MAGIC 0x4E504B51
#define NPK_RESPONSE_MAGIC 0x4E504B52

#define NPK_EXEC_SLOTS 8
#define NPK_EXEC_RECORD_SIZE 144
#define NPK_PAGE_SIZE 0x1000
#define NPK_SOURCE_SIZE 0xB000
#define NPK_NOB_HEADER_SIZE 32
#define NPK_NOB_MAGIC_WORD 0x56424F4E
#define NPK_NOB_VERSION 1
#define NPK_TARGET_ARCHITECTURE 1

#define NPK_OP_EXEC_LOAD 38
#define NPK_OP_EXEC_START 39
#define NPK_OP_EXEC_QUERY 40
#define NPK_OP_EXEC_RELEASE 41

#define NPK_IMAGE_FREE 0
#define NPK_IMAGE_LOADED 1
#define NPK_IMAGE_RUNNING 2
#define NPK_IMAGE_EXITED 3
#define NPK_IMAGE_FAILED 4

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

#define NPK_OK 0
#define NPK_REASON_NONE 0
#define NPK_REASON_BAD_ID 1
#define NPK_REASON_BAD_TYPE 2
#define NPK_REASON_DUPLICATE_ID 3
#define NPK_REASON_NO_SLOT 4
#define NPK_REASON_UNIT_UNAVAILABLE 5
#define NPK_REASON_NO_RECEIPT 6
#define NPK_REASON_EXEC_BAD_REQUEST 94
#define NPK_REASON_EXEC_BAD_CONTAINER 95
#define NPK_REASON_EXEC_BAD_SOURCE 96
#define NPK_REASON_EXEC_BAD_IMAGE 97
#define NPK_REASON_EXEC_BAD_ENTRY 98
#define NPK_REASON_EXEC_BAD_BASE 99
#define NPK_REASON_EXEC_BAD_SIZE 100
#define NPK_REASON_EXEC_BAD_ALIGN 101
#define NPK_REASON_EXEC_BAD_HANDLE 102
#define NPK_REASON_EXEC_BAD_OBJECT 103
#define NPK_REASON_EXEC_BAD_OWNER 104
#define NPK_REASON_EXEC_BAD_PERMISSION 105
#define NPK_REASON_EXEC_BAD_TASK 106
#define NPK_REASON_EXEC_BAD_STATE 107
#define NPK_REASON_EXEC_UNSUPPORTED 108
#define NPK_REASON_EXEC_EXHAUSTED 109
#define NPK_REASON_EXEC_BAD_MAPPING 110
#define NPK_REASON_EXEC_BAD_CONTEXT 111
#define NPK_REASON_EXEC_LAUNCH_FAILED 112
#define NPK_REASON_EXEC_TRAP 113
#define NPK_REASON_EXEC_COMMIT 114
#define NPK_REASON_EXEC_STALE 115

#define NPK_OBJECT_TYPE_MEMORY_REGION 2
#define NPK_OBJ_PERM_EXEC 0x04

#define NPK_EXEC_ID_BASE 0x4E504B4500000000
#define NPK_USER_PRIVILEGE 0
#define NPK_PTBR_ACTIVE 0x8000000000000000ULL
#define NPK_EXEC_STATUS_OK 1

struct NpkExecState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 init_status;
    npk_u64 exec_slots;
    npk_u64 exec_count;
    npk_u64 loaded_count;
    npk_u64 exited_count;
    npk_u64 next_image_id;
    npk_u64 next_generation;
    npk_u64 dispatch_count;
    npk_u64 launch_count;
    npk_u64 last_status;
    npk_u64 last_reason;
    npk_u64 last_image;
    npk_u64 last_task;
    npk_u64 target_architecture;
};

struct NpkExecRecord {
    npk_u64 state;
    npk_u64 id;
    npk_u64 generation;
    npk_u64 owner;
    npk_u64 handle;
    npk_u64 region_id;
    npk_u64 source_address;
    npk_u64 image_base;
    npk_u64 image_size;
    npk_u64 entry;
    npk_u64 task_id;
    npk_u64 asid;
    npk_u64 stack_base;
    npk_u64 exit_code;
    npk_u64 exit_cause;
    npk_u64 exit_ecause;
    npk_u64 launch_count;
    npk_u64 flags;
};

struct NpkExecRequest {
    npk_u64 magic;
    npk_u64 operation;
    npk_u64 id;
    npk_u64 image_id;
    npk_u64 owner;
    npk_u64 handle;
    npk_u64 source;
    npk_u64 source_size;
    npk_u64 task;
    npk_u64 asid;
    npk_u64 arg;
    npk_u64 flags;
};

struct NpkExecResponse {
    npk_u64 magic;
    npk_u64 status;
    npk_u64 operation;
    npk_u64 reason;
    npk_u64 result;
    npk_u64 image_id;
    npk_u64 receipt_index;
    npk_u64 slot;
};

struct NpkLaunchContext {
    npk_u64 status;
    npk_u64 record_index;
    npk_u64 vpc;
    npk_u64 vpriv;
    npk_u64 vptbr;
    npk_u64 guest_sp;
    npk_u64 guest_arg;
    npk_u64 stack_top;
    npk_u64 exit_code;
    npk_u64 exit_cause;
    npk_u64 exit_ecause;
    npk_u64 launch_count;
    npk_u64 saved_pc;
    npk_u64 saved_priv;
    npk_u64 saved_ptbr;
    npk_u64 flags;
};

struct NpkNobHeader {
    npk_u64 magic_version_flags;
    npk_u64 image_base;
    npk_u64 entry;
    npk_u64 image_size;
};

extern int npk_core_admit(npk_u64 type, npk_u64 id, npk_u64 input,
                          npk_u64 *out_slot);
extern int npk_core_advance(npk_u64 slot);
extern int npk_core_release(npk_u64 slot);
extern int npk_core_operation_outcome(npk_u64 slot);
extern npk_u64 npk_core_operation_result(npk_u64 slot);
extern npk_u64 npk_core_operation_reason(npk_u64 slot);
extern npk_u64 npk_core_operation_receipt_index(npk_u64 slot);

npk_u64 npk_exec_validate(struct NpkOperation *op);
npk_u64 npk_exec_execute(struct NpkOperation *op);
npk_u64 npk_exec_commit(struct NpkOperation *op);

static struct NpkExecState *npk_exec_state(void)
{
    return (struct NpkExecState *)NPK_EXEC_STATE_ADDRESS;
}

static struct NpkExecRecord *npk_exec_record(npk_u64 index)
{
    return (struct NpkExecRecord *)(NPK_EXEC_TABLE_ADDRESS +
        index * NPK_EXEC_RECORD_SIZE);
}

static struct NpkExecRequest *npk_exec_request(void)
{
    return (struct NpkExecRequest *)NPK_EXEC_REQUEST_ADDRESS;
}

static struct NpkExecResponse *npk_exec_response(void)
{
    return (struct NpkExecResponse *)NPK_EXEC_RESPONSE_ADDRESS;
}

static struct NpkLaunchContext *npk_exec_launch_context(void)
{
    return (struct NpkLaunchContext *)NPK_EXEC_LAUNCH_CONTEXT_ADDRESS;
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

static int npk_is_canonical(npk_u64 address)
{
    npk_u64 high = address >> 47;

    return high == 0 || high == 0x1FFFF;
}

static npk_u64 npk_exec_find_index(npk_u64 image_id)
{
    npk_u64 i;
    struct NpkExecRecord *record;

    if (image_id == 0) {
        return NPK_NO_INDEX;
    }
    for (i = 0; i < NPK_EXEC_SLOTS; i = i + 1) {
        record = npk_exec_record(i);
        if (record->id == image_id && record->state != NPK_IMAGE_FREE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_exec_find_free(void)
{
    npk_u64 i;
    for (i = 0; i < NPK_EXEC_SLOTS; i = i + 1) {
        if (npk_exec_record(i)->state == NPK_IMAGE_FREE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static int npk_exec_parse_header(npk_u64 source, npk_u64 source_size,
                                 struct NpkNobHeader *header,
                                 npk_u64 *version, npk_u64 *flags)
{
    if (source < NPK_EXEC_SOURCE_ADDRESS ||
            (source & 7) != 0 ||
            source_size < NPK_NOB_HEADER_SIZE ||
            source_size > NPK_SOURCE_SIZE ||
            source - NPK_EXEC_SOURCE_ADDRESS + source_size > NPK_SOURCE_SIZE) {
        return 0;
    }
    *header = *((struct NpkNobHeader *)source);
    *version = (header->magic_version_flags >> 32) & 0xFFFF;
    *flags = (header->magic_version_flags >> 48) & 0xFFFF;
    return 1;
}

static npk_u64 npk_exec_check_image_fields(struct NpkNobHeader *header,
                                           npk_u64 version, npk_u64 flags,
                                           npk_u64 source_size)
{
    if ((header->magic_version_flags & 0xFFFFFFFF) != NPK_NOB_MAGIC_WORD ||
            version != NPK_NOB_VERSION || flags != 0) {
        return NPK_REASON_EXEC_BAD_CONTAINER;
    }
    if (header->image_size == 0 || header->image_size > NPK_PAGE_SIZE) {
        return NPK_REASON_EXEC_BAD_IMAGE;
    }
    if ((header->image_size & 3) != 0 ||
            (header->image_base & (NPK_PAGE_SIZE - 1)) != 0 ||
            (header->entry & 3) != 0) {
        return NPK_REASON_EXEC_BAD_ALIGN;
    }
    if (header->image_size != source_size - NPK_NOB_HEADER_SIZE) {
        return NPK_REASON_EXEC_BAD_SIZE;
    }
    if (npk_is_canonical(header->image_base) == 0 ||
            header->image_base + NPK_PAGE_SIZE > 0x0000800000000000) {
        return NPK_REASON_EXEC_BAD_BASE;
    }
    if (header->entry < header->image_base ||
            header->entry >= header->image_base + header->image_size) {
        return NPK_REASON_EXEC_BAD_ENTRY;
    }
    return NPK_REASON_NONE;
}

static npk_u64 npk_exec_check_handle(npk_u64 handle, npk_u64 owner,
                                     npk_u64 region_id)
{
    npk_u64 type;
    npk_u64 permissions;
    npk_u64 payload;
    npk_u64 refcount;

    if (owner < 1 || owner > 2) {
        return NPK_REASON_EXEC_BAD_OWNER;
    }
    if (npk_object_handle_details(handle, owner, &type, &permissions,
                                  &payload, &refcount) == 0) {
        return NPK_REASON_EXEC_BAD_HANDLE;
    }
    if (type != NPK_OBJECT_TYPE_MEMORY_REGION) {
        return NPK_REASON_EXEC_BAD_OBJECT;
    }
    if ((permissions & NPK_OBJ_PERM_EXEC) == 0) {
        return NPK_REASON_EXEC_BAD_PERMISSION;
    }
    if (region_id != 0 && payload != region_id) {
        return NPK_REASON_EXEC_BAD_OBJECT;
    }
    return NPK_REASON_NONE;
}

/* Resolve the M5 handle to its M4 region through read-only services. The
 * object payload is a region id, never a physical address. */
static npk_u64 npk_exec_region_from_handle(npk_u64 handle, npk_u64 owner,
                                           npk_u64 *out_region_id,
                                           npk_u64 *out_base,
                                           npk_u64 *out_size,
                                           npk_u64 *out_reserved)
{
    npk_u64 reason = npk_exec_check_handle(handle, owner, 0);

    if (reason != NPK_REASON_NONE) {
        return reason;
    }
    *out_region_id = npk_object_payload_for_handle(handle, owner);
    if (*out_region_id == NPK_NO_INDEX ||
            npk_memory_region_info(*out_region_id, owner, out_base,
                                   out_size, out_reserved) == 0) {
        return NPK_REASON_EXEC_BAD_OBJECT;
    }
    return NPK_REASON_NONE;
}

static npk_u64 npk_exec_validate_load(struct NpkOperation *op)
{
    struct NpkNobHeader header;
    npk_u64 version;
    npk_u64 flags;
    npk_u64 reason;
    npk_u64 region_id;
    npk_u64 base;
    npk_u64 size;
    npk_u64 reserved;

    if (op->exec_flags != 0 || op->exec_image_id != 0) {
        return NPK_REASON_EXEC_BAD_REQUEST;
    }
    reason = npk_exec_region_from_handle(op->exec_handle, op->exec_owner,
                                         &region_id, &base, &size, &reserved);
    if (reason != NPK_REASON_NONE) {
        return reason;
    }
    if (npk_exec_parse_header(op->exec_source, op->exec_source_size, &header,
                              &version, &flags) == 0) {
        return NPK_REASON_EXEC_BAD_SOURCE;
    }
    reason = npk_exec_check_image_fields(&header, version, flags,
                                         op->exec_source_size);
    if (reason != NPK_REASON_NONE) {
        return reason;
    }
    if (size < header.image_size || reserved != 0) {
        return NPK_REASON_EXEC_BAD_STATE;
    }
    return NPK_REASON_NONE;
}

static npk_u64 npk_exec_validate_start(struct NpkOperation *op)
{
    struct NpkExecState *state = npk_exec_state();
    npk_u64 index = npk_exec_find_index(op->exec_image_id);
    struct NpkExecRecord *record;
    npk_u64 root;
    npk_u64 mappings;
    npk_u64 reason;
    npk_u64 code_physical;
    npk_u64 stack_physical;
    npk_u64 stack_base;
    npk_u64 region_id;
    npk_u64 region_base;
    npk_u64 region_size;
    npk_u64 reserved;

    if (op->exec_flags != 0 || index == NPK_NO_INDEX) {
        return NPK_REASON_EXEC_BAD_REQUEST;
    }
    record = npk_exec_record(index);
    if (record->owner != op->exec_owner) {
        return NPK_REASON_EXEC_BAD_OWNER;
    }
    if (record->state != NPK_IMAGE_LOADED || op->exec_task_id == 0) {
        return NPK_REASON_EXEC_BAD_STATE;
    }
    reason = npk_exec_region_from_handle(op->exec_handle, op->exec_owner,
                                         &region_id, &region_base,
                                         &region_size, &reserved);
    if (reason != NPK_REASON_NONE) {
        return reason;
    }
    if (region_id != record->region_id) {
        return NPK_REASON_EXEC_BAD_OBJECT;
    }
    if (npk_task_can_bind_execution(op->exec_task_id, op->exec_owner) !=
            NPK_REASON_NONE) {
        return NPK_REASON_EXEC_BAD_TASK;
    }
    if (op->exec_asid >= 2 ||
            npk_memory_address_space_info(op->exec_asid, op->exec_owner,
                                          &root, &mappings) == 0 ||
            root == 0 || mappings < 2) {
        return NPK_REASON_EXEC_BAD_MAPPING;
    }
    stack_base = record->image_base + NPK_PAGE_SIZE;
    if (npk_is_canonical(stack_base) == 0 ||
            stack_base + NPK_PAGE_SIZE > 0x0000800000000000) {
        return NPK_REASON_EXEC_BAD_BASE;
    }
    if (npk_memory_user_physical_in_as(op->exec_asid, op->exec_owner,
            record->image_base, 1 | 4, &code_physical) != NPK_REASON_NONE ||
            code_physical != region_base) {
        return NPK_REASON_EXEC_BAD_MAPPING;
    }
    if (npk_memory_user_physical_in_as(op->exec_asid, op->exec_owner,
            stack_base, 1 | 2, &stack_physical) != NPK_REASON_NONE) {
        return NPK_REASON_EXEC_BAD_MAPPING;
    }
    if (state->launch_count >= 64) {
        return NPK_REASON_EXEC_EXHAUSTED;
    }
    return NPK_REASON_NONE;
}

static npk_u64 npk_exec_validate_record_op(struct NpkOperation *op,
                                           npk_u64 *out_index)
{
    npk_u64 index = npk_exec_find_index(op->exec_image_id);
    struct NpkExecRecord *record;

    if (op->exec_flags != 0 || index == NPK_NO_INDEX) {
        return NPK_REASON_EXEC_BAD_REQUEST;
    }
    record = npk_exec_record(index);
    if (record->owner != op->exec_owner) {
        return NPK_REASON_EXEC_BAD_OWNER;
    }
    *out_index = index;
    return NPK_REASON_NONE;
}

npk_u64 npk_exec_validate(struct NpkOperation *op)
{
    if (op->type == NPK_OP_EXEC_LOAD) {
        return npk_exec_validate_load(op);
    }
    if (op->type == NPK_OP_EXEC_START) {
        return npk_exec_validate_start(op);
    }
    if (op->type == NPK_OP_EXEC_QUERY) {
        npk_u64 index;
        return npk_exec_validate_record_op(op, &index);
    }
    if (op->type == NPK_OP_EXEC_RELEASE) {
        npk_u64 index = npk_exec_find_index(op->exec_image_id);
        struct NpkExecRecord *record;
        npk_u64 reason = npk_exec_validate_record_op(op, &index);

        if (reason != NPK_REASON_NONE) {
            return reason;
        }
        record = npk_exec_record(index);
        if (record->state == NPK_IMAGE_RUNNING) {
            return NPK_REASON_EXEC_BAD_STATE;
        }
        return NPK_REASON_NONE;
    }
    return NPK_REASON_EXEC_UNSUPPORTED;
}

npk_u64 npk_exec_execute(struct NpkOperation *op)
{
    npk_u64 index;

    if (op->type == NPK_OP_EXEC_LOAD) {
        struct NpkExecState *state = npk_exec_state();
        index = npk_exec_find_free();
        if (index == NPK_NO_INDEX) {
            return NPK_REASON_EXEC_EXHAUSTED;
        }
        op->provisional = NPK_EXEC_ID_BASE | state->next_image_id;
        op->exec_evidence = (state->next_generation << 32) | index;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_EXEC_START) {
        struct NpkExecRecord *record;
        struct NpkLaunchContext *context = npk_exec_launch_context();
        npk_u64 root;
        npk_u64 mappings;

        index = npk_exec_find_index(op->exec_image_id);
        if (index == NPK_NO_INDEX) {
            return NPK_REASON_EXEC_BAD_REQUEST;
        }
        record = npk_exec_record(index);
        if (npk_memory_address_space_info(op->exec_asid, op->exec_owner,
                &root, &mappings) == 0) {
            return NPK_REASON_EXEC_BAD_MAPPING;
        }
        npk_clear_words((npk_u64 *)context,
                        sizeof(struct NpkLaunchContext) / 8);
        context->record_index = index;
        context->vpc = record->entry;
        context->vpriv = NPK_USER_PRIVILEGE;
        context->vptbr = NPK_PTBR_ACTIVE | (root & ~NPK_PTBR_ACTIVE);
        context->guest_sp = record->image_base + 2 * NPK_PAGE_SIZE - 16;
        context->guest_arg = op->exec_arg;
        context->stack_top = record->image_base + 2 * NPK_PAGE_SIZE;
        context->launch_count = record->launch_count + 1;
        op->provisional = NPK_TARGET_ARCHITECTURE;
        op->exec_evidence = (record->generation << 32) | index;
        return NPK_REASON_NONE;
    }
    index = npk_exec_find_index(op->exec_image_id);
    if (index == NPK_NO_INDEX) {
        return NPK_REASON_EXEC_BAD_REQUEST;
    }
    op->provisional = 1;
    op->exec_evidence = (npk_exec_record(index)->generation << 32) | index;
    return NPK_REASON_NONE;
}

npk_u64 npk_exec_commit(struct NpkOperation *op)
{
    struct NpkExecState *state = npk_exec_state();
    npk_u64 index;
    struct NpkExecRecord *record;
    struct NpkNobHeader header;
    npk_u64 version;
    npk_u64 flags;
    npk_u64 i;
    npk_u64 status;
    npk_u64 reason;
    npk_u64 region_id;
    npk_u64 region_base;
    npk_u64 region_size;
    npk_u64 reserved;

    if (op->type == NPK_OP_EXEC_LOAD) {
        index = npk_exec_find_free();
        if (index == NPK_NO_INDEX) {
            return NPK_REASON_EXEC_EXHAUSTED;
        }
        if (npk_exec_parse_header(op->exec_source, op->exec_source_size,
                &header, &version, &flags) == 0) {
            return NPK_REASON_EXEC_BAD_SOURCE;
        }
        reason = npk_exec_region_from_handle(op->exec_handle, op->exec_owner,
                     &region_id, &region_base, &region_size, &reserved);
        if (reason != NPK_REASON_NONE) {
            return reason;
        }
        record = npk_exec_record(index);
        npk_clear_words((npk_u64 *)record, NPK_EXEC_RECORD_SIZE / 8);
        for (i = 0; i < header.image_size; i = i + 1) {
            npk_store_byte(region_base + i,
                           npk_load_byte(op->exec_source +
                                         NPK_NOB_HEADER_SIZE + i));
        }
        record->state = NPK_IMAGE_LOADED;
        record->id = op->provisional;
        record->generation = state->next_generation == 0 ? 1 : state->next_generation;
        record->owner = op->exec_owner;
        record->handle = op->exec_handle;
        record->region_id = region_id;
        record->source_address = op->exec_source;
        record->image_base = header.image_base;
        record->image_size = header.image_size;
        record->entry = header.entry;
        record->flags = NPK_TARGET_ARCHITECTURE;
        op->exec_evidence = (record->generation << 32) | index;
        state->next_image_id = state->next_image_id + 1;
        state->next_generation = record->generation + 1;
        state->exec_count = state->exec_count + 1;
        state->loaded_count = state->loaded_count + 1;
        state->last_image = record->id;
        op->result = record->id;
        return NPK_REASON_NONE;
    }

    index = npk_exec_find_index(op->exec_image_id);
    if (index == NPK_NO_INDEX) {
        return NPK_REASON_EXEC_STALE;
    }
    record = npk_exec_record(index);

    if (op->type == NPK_OP_EXEC_START) {
        struct NpkLaunchContext *context = npk_exec_launch_context();

        reason = npk_task_bind_execution(op->exec_task_id, op->exec_owner,
                     record->entry, record->image_base + NPK_PAGE_SIZE,
                     NPK_PAGE_SIZE, op->exec_arg, op->exec_asid);
        if (reason != NPK_REASON_NONE) {
            op->exec_error = reason;
            return NPK_REASON_EXEC_COMMIT;
        }
        record->task_id = op->exec_task_id;
        record->asid = op->exec_asid;
        record->state = NPK_IMAGE_RUNNING;
        record->launch_count = record->launch_count + 1;
        state->launch_count = state->launch_count + 1;
        state->last_task = op->exec_task_id;
        status = npk_exec_vmlaunch();
        record->exit_code = context->exit_code;
        record->exit_cause = context->exit_cause;
        record->exit_ecause = context->exit_ecause;
        op->exec_error = context->exit_cause;
        npk_task_finish_execution(op->exec_task_id, context->exit_code);
        if (status != 0 || context->status == 0 ||
                context->exit_cause != 7) {
            record->state = NPK_IMAGE_FAILED;
            op->exec_error = NPK_REASON_EXEC_TRAP;
            return NPK_REASON_EXEC_LAUNCH_FAILED;
        }
        record->state = NPK_IMAGE_EXITED;
        state->exited_count = state->exited_count + 1;
        state->loaded_count = state->loaded_count - 1;
        state->last_image = record->id;
        op->result = record->exit_code;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_EXEC_QUERY) {
        op->result = record->state == NPK_IMAGE_EXITED ?
            record->exit_code : record->state;
        return NPK_REASON_NONE;
    }

    if (op->type != NPK_OP_EXEC_RELEASE ||
            record->state == NPK_IMAGE_RUNNING) {
        return NPK_REASON_EXEC_UNSUPPORTED;
    }
    status = record->state;
    npk_clear_words((npk_u64 *)record, NPK_EXEC_RECORD_SIZE / 8);
    state->exec_count = state->exec_count - 1;
    if (status == NPK_IMAGE_LOADED) {
        state->loaded_count = state->loaded_count - 1;
    }
    op->result = op->exec_image_id;
    return NPK_REASON_NONE;
}

static void npk_exec_write_response(npk_u64 status, npk_u64 operation,
                                    npk_u64 reason, npk_u64 result,
                                    npk_u64 image_id, npk_u64 receipt_index,
                                    npk_u64 slot)
{
    struct NpkExecResponse *response = npk_exec_response();

    response->magic = NPK_RESPONSE_MAGIC;
    response->status = status;
    response->operation = operation;
    response->reason = reason;
    response->result = result;
    response->image_id = image_id;
    response->receipt_index = receipt_index;
    response->slot = slot;
}

int npk_exec_submit_request(void)
{
    struct NpkExecState *state = npk_exec_state();
    struct NpkExecRequest *request = npk_exec_request();
    struct NpkExecArguments args;
    npk_u64 slot = NPK_NO_INDEX;
    npk_u64 reason;
    npk_u64 result;
    npk_u64 status;
    npk_u64 admission;
    npk_u64 i;

    state->dispatch_count = state->dispatch_count + 1;
    npk_exec_write_response(0, 0, 0, 0, 0, NPK_NO_INDEX, NPK_NO_INDEX);
    if (request->magic != NPK_REQUEST_MAGIC || request->id == 0 ||
            request->operation < NPK_OP_EXEC_LOAD ||
            request->operation > NPK_OP_EXEC_RELEASE) {
        state->last_status = NPK_REQUEST_BAD_REQUEST;
        state->last_reason = NPK_REASON_EXEC_BAD_REQUEST;
        npk_exec_write_response(state->last_status, request->operation,
                                state->last_reason, 0, request->image_id,
                                NPK_NO_INDEX, NPK_NO_INDEX);
        return (int)state->last_status;
    }
    admission = npk_core_admit(request->operation, request->id,
                               request->image_id, &slot);
    if (admission != NPK_OK) {
        if (admission == NPK_ERR_NO_SLOT ||
                admission == NPK_ERR_NO_RECEIPT ||
                admission == NPK_ERR_UNIT_UNAVAILABLE) {
            reason = NPK_REASON_EXEC_EXHAUSTED;
            status = NPK_REQUEST_ADMIT_FAIL;
        } else if (admission == NPK_ERR_DUPLICATE_ID) {
            reason = NPK_REASON_EXEC_STALE;
            status = NPK_REQUEST_ADMIT_FAIL;
        } else {
            reason = NPK_REASON_EXEC_BAD_REQUEST;
            status = NPK_REQUEST_ADMIT_FAIL;
        }
        state->last_status = status;
        state->last_reason = reason;
        npk_exec_write_response(status, request->operation, reason, 0,
                                request->image_id, NPK_NO_INDEX, slot);
        return (int)status;
    }
    args.owner = request->owner;
    args.handle = request->handle;
    args.source = request->source;
    args.source_size = request->source_size;
    args.image_id = request->image_id;
    args.task_id = request->task;
    args.asid = request->asid;
    args.arg = request->arg;
    args.flags = request->flags;
    npk_core_set_exec_arguments(slot, &args);
    for (i = 0; i < 5; i = i + 1) {
        if (npk_core_advance(slot) != NPK_OK) {
            state->last_status = NPK_REQUEST_ADVANCE_FAIL;
            state->last_reason = NPK_REASON_EXEC_BAD_CONTEXT;
            npk_exec_write_response(state->last_status, request->operation,
                                    state->last_reason, 0, request->image_id,
                                    NPK_NO_INDEX, slot);
            npk_core_release(slot);
            return (int)state->last_status;
        }
        if (npk_core_operation_outcome(slot) != NPK_OUTCOME_PENDING) {
            break;
        }
    }
    reason = npk_core_operation_reason(slot);
    result = npk_core_operation_result(slot);
    if (npk_core_operation_outcome(slot) == NPK_OUTCOME_COMPLETE) {
        status = NPK_REQUEST_OK;
    } else if (npk_core_operation_outcome(slot) == NPK_OUTCOME_REJECTED) {
        status = NPK_REQUEST_REJECTED;
    } else if (npk_core_operation_outcome(slot) == NPK_OUTCOME_FAILED) {
        status = NPK_REQUEST_FAILED;
    } else {
        status = NPK_REQUEST_BAD_OUTCOME;
    }
    state->last_status = status;
    state->last_reason = reason;
    state->last_image = request->image_id;
    npk_exec_write_response(status, request->operation, reason, result,
                            request->image_id,
                            npk_core_operation_receipt_index(slot), slot);
    npk_core_release(slot);
    return (int)status;
}

int npk_exec_init(void)
{
    struct NpkExecState *state = npk_exec_state();
    npk_u64 i;

    npk_clear_words((npk_u64 *)state, sizeof(struct NpkExecState) / 8);
    for (i = 0; i < NPK_EXEC_SLOTS; i = i + 1) {
        npk_clear_words((npk_u64 *)npk_exec_record(i),
                        NPK_EXEC_RECORD_SIZE / 8);
    }
    npk_clear_words((npk_u64 *)npk_exec_request(),
                    sizeof(struct NpkExecRequest) / 8);
    npk_clear_words((npk_u64 *)npk_exec_response(),
                    sizeof(struct NpkExecResponse) / 8);
    npk_clear_words((npk_u64 *)npk_exec_launch_context(),
                    sizeof(struct NpkLaunchContext) / 8);
    npk_clear_words((npk_u64 *)0xB4880, 8);
    state->magic = NPK_EXEC_STATE_MAGIC;
    state->version = NPK_EXEC_STATE_VERSION;
    state->exec_slots = NPK_EXEC_SLOTS;
    state->next_image_id = 1;
    state->next_generation = 1;
    state->target_architecture = NPK_TARGET_ARCHITECTURE;
    return 0;
}
