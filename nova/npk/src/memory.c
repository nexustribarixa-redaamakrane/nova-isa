/*
 * NPK Milestone 4 physical and virtual memory processing.
 *
 * NovaMachine has sparse byte memory and a real four-level translator, but no
 * RAM-size model, frame allocator, address-space object, or map API. NPK
 * therefore declares a finite physical arena and implements ownership,
 * allocation, page-table construction, and receipts itself. Every supported
 * operation is admitted to the existing processing core and traverses the full
 * ADMIT -> VALIDATE -> EXECUTE -> COMMIT -> COMPLETE lifecycle.
 *
 * This is a machine-mode kernel subsystem over the reference model. It is not
 * a claim of physical hardware support, ASIDs, TLB shootdown, or transactional
 * rollback.
 */

#include "npk_core.h"

#define NPK_MEMORY_STATE_ADDRESS 0x90F10
#define NPK_MEMORY_REGION_TABLE_ADDRESS 0x90F90
#define NPK_ADDRESS_SPACE_TABLE_ADDRESS 0x91F90
#define NPK_MEMORY_REQUEST_ADDRESS 0x91FD0
#define NPK_MEMORY_RESPONSE_ADDRESS 0x92010

#define NPK_MEMORY_STATE_MAGIC 0x4E504B4D
#define NPK_MEMORY_STATE_VERSION 4
#define NPK_MEMORY_REQUEST_MAGIC 0x4E504B51
#define NPK_MEMORY_RESPONSE_MAGIC 0x4E504B5A

#define NPK_OP_MEMORY_ALLOCATE 4
#define NPK_OP_MEMORY_RELEASE 5
#define NPK_OP_MEMORY_MAP 6
#define NPK_OP_MEMORY_UNMAP 7
#define NPK_OP_MEMORY_QUERY 8

#define NPK_REASON_NONE 0
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

#define NPK_OUTCOME_PENDING 1
#define NPK_OUTCOME_COMPLETE 2
#define NPK_OUTCOME_REJECTED 3
#define NPK_OUTCOME_FAILED 4

#define NPK_REQUEST_OK 0
#define NPK_REQUEST_BAD_REQUEST 1
#define NPK_REQUEST_BAD_OP 2
#define NPK_REQUEST_ADMIT_FAIL 3
#define NPK_REQUEST_ADVANCE_FAIL 4
#define NPK_REQUEST_BAD_OUTCOME 5
#define NPK_REQUEST_REJECTED 6
#define NPK_REQUEST_FAILED 7

#define NPK_NO_INDEX 0xFFFFFFFFFFFFFFFF
#define NPK_ARENA_BASE 0xC0000
#define NPK_ARENA_SIZE 0x40000
#define NPK_PAGE_SIZE 0x1000
#define NPK_REGION_SLOTS 64
#define NPK_ADDRESS_SPACE_SLOTS 2
#define NPK_PTE_PPN_MASK 0x000FFFFFFFFFF000
#define NPK_PAGE_TABLE_ENTRIES 512

#define NPK_REGION_FREE 0
#define NPK_REGION_ALLOCATED 1
#define NPK_REGION_PAGE_TABLE 2

#define NPK_REGION_TYPE_NORMAL 1
#define NPK_REGION_TYPE_PAGE_TABLE 2

#define NPK_ADDRESS_SPACE_FREE 0
#define NPK_ADDRESS_SPACE_ACTIVE 1

#define NPK_PERM_READ 1
#define NPK_PERM_WRITE 2
#define NPK_PERM_EXEC 4
#define NPK_PERM_USER 8

#define NPK_PTE_VALID 0x1
#define NPK_PTE_READ 0x2
#define NPK_PTE_WRITE 0x4
#define NPK_PTE_EXEC 0x8
#define NPK_PTE_USER 0x10

struct NpkMemoryState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 init_status;
    npk_u64 arena_base;
    npk_u64 arena_size;
    npk_u64 page_size;
    npk_u64 region_slots;
    npk_u64 region_count;
    npk_u64 address_space_slots;
    npk_u64 mapping_count;
    npk_u64 next_physical;
    npk_u64 allocation_epoch;
    npk_u64 invalidation_epoch;
    npk_u64 next_region_id;
    npk_u64 last_status;
    npk_u64 last_reason;
};

struct NpkMemoryRegion {
    npk_u64 state;
    npk_u64 id;
    npk_u64 base;
    npk_u64 size;
    npk_u64 owner;
    npk_u64 type;
    npk_u64 flags;
    npk_u64 reserved;
};

struct NpkAddressSpace {
    npk_u64 state;
    npk_u64 owner;
    npk_u64 root;
    npk_u64 mapping_count;
};

struct NpkMemoryRequest {
    npk_u64 magic;
    npk_u64 operation;
    npk_u64 id;
    npk_u64 owner;
    npk_u64 address;
    npk_u64 size;
    npk_u64 flags;
    npk_u64 aux;
};

struct NpkMemoryResponse {
    npk_u64 magic;
    npk_u64 status;
    npk_u64 operation;
    npk_u64 reason;
    npk_u64 result;
    npk_u64 aux;
    npk_u64 receipt_index;
    npk_u64 slot;
};

extern int npk_core_admit(npk_u64 type, npk_u64 id, npk_u64 input,
                          npk_u64 *out_slot);
extern int npk_core_advance(npk_u64 slot);
extern int npk_core_release(npk_u64 slot);
extern int npk_core_operation_outcome(npk_u64 slot);
extern npk_u64 npk_core_operation_result(npk_u64 slot);
extern npk_u64 npk_core_operation_reason(npk_u64 slot);
extern npk_u64 npk_core_operation_receipt_index(npk_u64 slot);
extern void npk_core_set_memory_arguments(npk_u64 slot, npk_u64 address,
                                          npk_u64 size, npk_u64 flags,
                                          npk_u64 aux);
extern void npk_memory_flush_tlb(void);

npk_u64 npk_memory_validate(struct NpkOperation *op);
npk_u64 npk_memory_execute(struct NpkOperation *op);
npk_u64 npk_memory_commit(struct NpkOperation *op);

static struct NpkMemoryState *npk_state(void)
{
    return (struct NpkMemoryState *)NPK_MEMORY_STATE_ADDRESS;
}

static struct NpkMemoryRegion *npk_region(npk_u64 index)
{
    return (struct NpkMemoryRegion *)(NPK_MEMORY_REGION_TABLE_ADDRESS +
        index * sizeof(struct NpkMemoryRegion));
}

static struct NpkAddressSpace *npk_address_space(npk_u64 index)
{
    return (struct NpkAddressSpace *)(NPK_ADDRESS_SPACE_TABLE_ADDRESS +
        index * sizeof(struct NpkAddressSpace));
}

static struct NpkMemoryRequest *npk_request(void)
{
    return (struct NpkMemoryRequest *)NPK_MEMORY_REQUEST_ADDRESS;
}

static struct NpkMemoryResponse *npk_response(void)
{
    return (struct NpkMemoryResponse *)NPK_MEMORY_RESPONSE_ADDRESS;
}

static int npk_is_power_of_two(npk_u64 value)
{
    if (value == 0) {
        return 0;
    }
    return (value & (value - 1)) == 0;
}

static npk_u64 npk_align_up(npk_u64 value, npk_u64 align)
{
    return (value + align - 1) & ~(align - 1);
}

static int npk_ranges_overlap(npk_u64 left_base, npk_u64 left_size,
                              npk_u64 right_base, npk_u64 right_size)
{
    if (left_size == 0 || right_size == 0) {
        return 0;
    }
    return left_base < right_base + right_size &&
        right_base < left_base + left_size;
}

static int npk_is_canonical(npk_u64 address)
{
    npk_u64 high = address >> 47;

    return high == 0 || high == 0x1FFFF;
}

static npk_u64 npk_find_region(npk_u64 id)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 i;

    if (id == 0) {
        return NPK_NO_INDEX;
    }
    for (i = 0; i < state->region_slots; i = i + 1) {
        if (npk_region(i)->state != NPK_REGION_FREE &&
                npk_region(i)->id == id) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_region_by_base(npk_u64 base)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 i;

    if (base == 0) {
        return NPK_NO_INDEX;
    }
    for (i = 0; i < state->region_slots; i = i + 1) {
        if (npk_region(i)->state != NPK_REGION_FREE &&
                npk_region(i)->base == base) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_free_region(void)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 i;

    for (i = 0; i < state->region_slots; i = i + 1) {
        if (npk_region(i)->state == NPK_REGION_FREE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_fit(npk_u64 size, npk_u64 align, npk_u64 *out)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 arena_end = state->arena_base + state->arena_size;
    npk_u64 candidate = npk_align_up(state->next_physical, align);
    npk_u64 i;
    npk_u64 conflict;
    struct NpkMemoryRegion *region;

    for (;;) {
        if (candidate < state->arena_base) {
            return NPK_REASON_MEMORY_EXHAUSTED;
        }
        if (candidate > arena_end || size > arena_end - candidate) {
            return NPK_REASON_MEMORY_EXHAUSTED;
        }
        conflict = NPK_NO_INDEX;
        for (i = 0; i < state->region_slots; i = i + 1) {
            region = npk_region(i);
            if (region->state == NPK_REGION_FREE) {
                continue;
            }
            if (npk_ranges_overlap(candidate, size,
                                   region->base, region->size)) {
                conflict = i;
                break;
            }
        }
        if (conflict == NPK_NO_INDEX) {
            *out = candidate;
            return NPK_REASON_NONE;
        }
        region = npk_region(conflict);
        candidate = npk_align_up(region->base + region->size, align);
    }
}

static npk_u64 npk_pte_flags(npk_u64 permissions)
{
    npk_u64 pte = NPK_PTE_VALID;

    if (permissions & NPK_PERM_READ) {
        pte = pte | NPK_PTE_READ;
    }
    if (permissions & NPK_PERM_WRITE) {
        pte = pte | NPK_PTE_WRITE;
    }
    if (permissions & NPK_PERM_EXEC) {
        pte = pte | NPK_PTE_EXEC;
    }
    if (permissions & NPK_PERM_USER) {
        pte = pte | NPK_PTE_USER;
    }
    return pte;
}

static npk_u64 npk_address_space_id(npk_u64 aux)
{
    return aux & 0xFFFF;
}

static npk_u64 npk_region_id_from_aux(npk_u64 aux)
{
    return aux >> 16;
}

static npk_u64 npk_page_table_index(npk_u64 address, npk_u64 level)
{
    npk_u64 shift = 48 - 9 * (level + 1);

    return (address >> shift) & 511;
}

static npk_u64 npk_table_address(npk_u64 root, npk_u64 index)
{
    return (root & NPK_PTE_PPN_MASK) | (index * 8);
}

static void npk_zero_page(npk_u64 page)
{
    npk_u64 i;

    for (i = 0; i < NPK_PAGE_TABLE_ENTRIES; i = i + 1) {
        *((npk_u64 *)page + i) = 0;
    }
}

int npk_memory_init(void)
{
    struct NpkMemoryState *state = npk_state();

    /* This is a one-time boot initializer. NovaMachine sparse memory and the
     * linker's zero-filled image already provide zeroed observation regions,
     * so clearing the whole region table at boot would only waste steps. */
    state->magic = NPK_MEMORY_STATE_MAGIC;
    state->version = NPK_MEMORY_STATE_VERSION;
    state->init_status = 0;
    state->arena_base = NPK_ARENA_BASE;
    state->arena_size = NPK_ARENA_SIZE;
    state->page_size = NPK_PAGE_SIZE;
    state->region_slots = NPK_REGION_SLOTS;
    state->region_count = 0;
    state->address_space_slots = NPK_ADDRESS_SPACE_SLOTS;
    state->mapping_count = 0;
    state->next_physical = NPK_ARENA_BASE;
    state->allocation_epoch = 0;
    state->invalidation_epoch = 0;
    state->next_region_id = 1;
    state->last_status = 0;
    state->last_reason = NPK_REASON_NONE;
    return 0;
}

static npk_u64 npk_validate_allocate(struct NpkOperation *op,
                                     struct NpkMemoryRequest *request)
{
    npk_u64 align = request->aux;

    if (request->address != 0 || request->flags != NPK_REGION_TYPE_NORMAL) {
        return NPK_REASON_MEMORY_BAD_REQUEST;
    }
    if (request->size == 0 || request->size > NPK_ARENA_SIZE ||
            request->size % NPK_PAGE_SIZE != 0) {
        return NPK_REASON_MEMORY_BAD_SIZE;
    }
    if (align == 0) {
        align = NPK_PAGE_SIZE;
    }
    if (align < NPK_PAGE_SIZE || npk_is_power_of_two(align) == 0) {
        return NPK_REASON_MEMORY_BAD_ALIGN;
    }
    op->memory_address = 0;
    op->memory_size = request->size;
    op->memory_flags = request->flags;
    op->memory_aux = align;
    return NPK_REASON_NONE;
}

static npk_u64 npk_validate_region_operation(struct NpkOperation *op,
                                             struct NpkMemoryRequest *request)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 index = npk_find_region(request->address);
    struct NpkMemoryRegion *region;

    if (index == NPK_NO_INDEX) {
        return NPK_REASON_MEMORY_BAD_REGION;
    }
    region = npk_region(index);
    if (region->state != NPK_REGION_ALLOCATED) {
        return NPK_REASON_MEMORY_STATE;
    }
    if (region->owner != request->owner) {
        return NPK_REASON_MEMORY_OWNER;
    }
    if (op->type == NPK_OP_MEMORY_RELEASE && region->reserved != 0) {
        return NPK_REASON_MEMORY_STATE;
    }
    if (request->size != 0 || request->flags != 0 || request->aux != 0) {
        return NPK_REASON_MEMORY_BAD_REQUEST;
    }
    op->memory_address = request->address;
    op->memory_size = 0;
    op->memory_flags = 0;
    op->memory_aux = index;
    return NPK_REASON_NONE;
}

static npk_u64 npk_validate_map(struct NpkOperation *op,
                                struct NpkMemoryRequest *request)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 asid = npk_address_space_id(request->aux);
    npk_u64 region_id = npk_region_id_from_aux(request->aux);
    npk_u64 index = npk_find_region(region_id);
    struct NpkMemoryRegion *region;
    struct NpkAddressSpace *space;
    npk_u64 permissions = request->flags;

    if (asid >= state->address_space_slots) {
        return NPK_REASON_MEMORY_BAD_ADDRESS_SPACE;
    }
    if (request->size != NPK_PAGE_SIZE) {
        return NPK_REASON_MEMORY_BAD_SIZE;
    }
    if (request->address % NPK_PAGE_SIZE != 0 ||
            npk_is_canonical(request->address) == 0) {
        return NPK_REASON_MEMORY_BAD_MAPPING;
    }
    if (permissions == 0 || permissions > 0xF) {
        return NPK_REASON_MEMORY_UNSUPPORTED;
    }
    if ((permissions & (NPK_PERM_WRITE | NPK_PERM_EXEC)) != 0 &&
            (permissions & NPK_PERM_READ) == 0) {
        return NPK_REASON_MEMORY_BAD_MAPPING;
    }
    if (index == NPK_NO_INDEX) {
        return NPK_REASON_MEMORY_BAD_REGION;
    }
    region = npk_region(index);
    if (region->state != NPK_REGION_ALLOCATED ||
            region->type != NPK_REGION_TYPE_NORMAL) {
        return NPK_REASON_MEMORY_STATE;
    }
    if (region->owner != request->owner || region->size < NPK_PAGE_SIZE) {
        return NPK_REASON_MEMORY_OWNER;
    }
    space = npk_address_space(asid);
    if (space->state != NPK_ADDRESS_SPACE_FREE && space->owner != request->owner) {
        return NPK_REASON_MEMORY_OWNER;
    }
    op->memory_address = request->address;
    op->memory_size = request->size;
    op->memory_flags = permissions;
    op->memory_aux = request->aux;
    return NPK_REASON_NONE;
}

static npk_u64 npk_validate_unmap(struct NpkOperation *op,
                                  struct NpkMemoryRequest *request)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 asid = npk_address_space_id(request->aux);
    struct NpkAddressSpace *space;

    if (asid >= state->address_space_slots) {
        return NPK_REASON_MEMORY_BAD_ADDRESS_SPACE;
    }
    space = npk_address_space(asid);
    if (space->state != NPK_ADDRESS_SPACE_ACTIVE || space->owner != request->owner) {
        return NPK_REASON_MEMORY_OWNER;
    }
    if (request->size != NPK_PAGE_SIZE || request->flags != 0 ||
            request->address % NPK_PAGE_SIZE != 0 ||
            npk_is_canonical(request->address) == 0) {
        return NPK_REASON_MEMORY_BAD_MAPPING;
    }
    if (space->mapping_count == 0) {
        return NPK_REASON_MEMORY_STATE;
    }
    op->memory_address = request->address;
    op->memory_size = request->size;
    op->memory_flags = 0;
    op->memory_aux = request->aux;
    return NPK_REASON_NONE;
}

npk_u64 npk_memory_validate(struct NpkOperation *op)
{
    struct NpkMemoryRequest *request = npk_request();

    if (op->input != NPK_MEMORY_REQUEST_ADDRESS ||
            request->magic != NPK_MEMORY_REQUEST_MAGIC ||
            request->id != op->id || request->operation != op->type) {
        return NPK_REASON_MEMORY_BAD_REQUEST;
    }
    if (request->owner == 0) {
        return NPK_REASON_MEMORY_OWNER;
    }
    if (op->type == NPK_OP_MEMORY_ALLOCATE) {
        return npk_validate_allocate(op, request);
    }
    if (op->type == NPK_OP_MEMORY_RELEASE ||
            op->type == NPK_OP_MEMORY_QUERY) {
        return npk_validate_region_operation(op, request);
    }
    if (op->type == NPK_OP_MEMORY_MAP) {
        return npk_validate_map(op, request);
    }
    if (op->type == NPK_OP_MEMORY_UNMAP) {
        return npk_validate_unmap(op, request);
    }
    return NPK_REASON_MEMORY_UNSUPPORTED;
}

npk_u64 npk_memory_execute(struct NpkOperation *op)
{
    struct NpkMemoryState *state = npk_state();
    struct NpkAddressSpace *space;
    npk_u64 asid;
    npk_u64 root;
    npk_u64 pte_address;
    npk_u64 pte;
    npk_u64 needed;
    npk_u64 level;
    npk_u64 candidate;
    npk_u64 reason;
    npk_u64 region_index;

    if (op->type == NPK_OP_MEMORY_ALLOCATE) {
        reason = npk_find_fit(op->memory_size, op->memory_aux, &candidate);
        if (reason != NPK_REASON_NONE) {
            return reason;
        }
        op->provisional = candidate;
        op->memory_evidence = op->memory_size;
        op->memory_scratch = op->memory_aux;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_MEMORY_RELEASE ||
            op->type == NPK_OP_MEMORY_QUERY) {
        struct NpkMemoryRegion *region;

        region = npk_region(op->memory_aux);
        if (region->state != NPK_REGION_ALLOCATED || region->id != op->memory_address) {
            return NPK_REASON_MEMORY_STATE;
        }
        if (op->type == NPK_OP_MEMORY_QUERY) {
            op->memory_evidence = region->size;
            op->memory_scratch = region->flags;
        } else {
            op->memory_evidence = region->id;
            op->memory_scratch = region->base;
        }
        op->provisional = region->base;
        return NPK_REASON_NONE;
    }

    asid = npk_address_space_id(op->memory_aux);
    space = npk_address_space(asid);

    if (op->type == NPK_OP_MEMORY_MAP) {
        root = space->root;
        needed = 0;
        if (root == 0) {
            needed = 4;
        } else {
            for (level = 0; level < 3; level = level + 1) {
                pte_address = npk_table_address(root,
                    npk_page_table_index(op->memory_address, level));
                pte = *((npk_u64 *)pte_address);
                if ((pte & 0xE) != 0) {
                    return NPK_REASON_MEMORY_PTE_CONFLICT;
                }
                if ((pte & NPK_PTE_VALID) == 0) {
                    needed = 3 - level;
                    break;
                }
                root = pte & NPK_PTE_PPN_MASK;
            }
        }
        if (npk_find_free_region() == NPK_NO_INDEX ||
                needed > (state->arena_base + state->arena_size -
                          state->next_physical) / NPK_PAGE_SIZE) {
            return NPK_REASON_MEMORY_EXHAUSTED;
        }
        op->provisional = op->memory_address;
        region_index = npk_find_region(npk_region_id_from_aux(op->memory_aux));
        if (region_index == NPK_NO_INDEX) {
            return NPK_REASON_MEMORY_BAD_REGION;
        }
        op->memory_evidence = npk_region(region_index)->base;
        op->memory_scratch = needed;
        op->memory_flags = npk_pte_flags(op->memory_flags);
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_MEMORY_UNMAP) {
        root = space->root;
        if (root == 0) {
            return NPK_REASON_MEMORY_BAD_MAPPING;
        }
        for (level = 0; level < 4; level = level + 1) {
            pte_address = npk_table_address(root,
                npk_page_table_index(op->memory_address, level));
            pte = *((npk_u64 *)pte_address);
            if ((pte & NPK_PTE_VALID) == 0) {
                return NPK_REASON_MEMORY_BAD_MAPPING;
            }
            if (level == 3) {
                if ((pte & 0xE) == 0) {
                    return NPK_REASON_MEMORY_BAD_MAPPING;
                }
                op->provisional = op->memory_address;
                op->memory_evidence = pte_address;
                op->memory_scratch = pte;
                return NPK_REASON_NONE;
            }
            if ((pte & 0xE) != 0) {
                return NPK_REASON_MEMORY_UNSUPPORTED;
            }
            root = pte & NPK_PTE_PPN_MASK;
        }
    }
    return NPK_REASON_MEMORY_UNSUPPORTED;
}

static npk_u64 npk_raw_allocate_page(npk_u64 *out)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 index = npk_find_free_region();
    npk_u64 candidate;
    npk_u64 reason;
    struct NpkMemoryRegion *region;

    if (index == NPK_NO_INDEX) {
        return NPK_REASON_MEMORY_EXHAUSTED;
    }
    reason = npk_find_fit(NPK_PAGE_SIZE, NPK_PAGE_SIZE, &candidate);
    if (reason != NPK_REASON_NONE) {
        return reason;
    }
    region = npk_region(index);
    region->state = NPK_REGION_PAGE_TABLE;
    region->id = state->next_region_id;
    region->base = candidate;
    region->size = NPK_PAGE_SIZE;
    region->owner = 0;
    region->type = NPK_REGION_TYPE_PAGE_TABLE;
    region->flags = 0;
    region->reserved = 0;
    state->next_region_id = state->next_region_id + 1;
    state->region_count = state->region_count + 1;
    state->next_physical = candidate + NPK_PAGE_SIZE;
    state->allocation_epoch = state->allocation_epoch + 1;
    npk_zero_page(candidate);
    *out = candidate;
    return NPK_REASON_NONE;
}

static npk_u64 npk_commit_allocate(struct NpkOperation *op)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 index = npk_find_free_region();
    npk_u64 expected;
    npk_u64 reason;
    struct NpkMemoryRegion *region;

    if (index == NPK_NO_INDEX) {
        return NPK_REASON_MEMORY_EXHAUSTED;
    }
    reason = npk_find_fit(op->memory_size, op->memory_scratch, &expected);
    if (reason != NPK_REASON_NONE || expected != op->provisional) {
        return NPK_REASON_MEMORY_OVERLAP;
    }
    region = npk_region(index);
    region->state = NPK_REGION_ALLOCATED;
    region->id = state->next_region_id;
    region->base = op->provisional;
    region->size = op->memory_size;
    region->owner = npk_request()->owner;
    region->type = NPK_REGION_TYPE_NORMAL;
    region->flags = 0;
    region->reserved = 0;
    state->next_region_id = state->next_region_id + 1;
    state->region_count = state->region_count + 1;
    state->next_physical = op->provisional + op->memory_size;
    state->allocation_epoch = state->allocation_epoch + 1;
    *((npk_u64 *)op->provisional) = 0x4E504B4D414C4C00 + region->id;
    op->result = op->provisional;
    op->memory_evidence = op->memory_size;
    return NPK_REASON_NONE;
}

static npk_u64 npk_commit_release(struct NpkOperation *op)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 index = npk_find_region(op->memory_evidence);
    struct NpkMemoryRegion *region;
    npk_u64 base;

    if (index == NPK_NO_INDEX) {
        return NPK_REASON_MEMORY_STATE;
    }
    region = npk_region(index);
    if (region->base != op->memory_scratch ||
            region->state != NPK_REGION_ALLOCATED) {
        return NPK_REASON_MEMORY_STATE;
    }
    base = region->base;
    region->state = NPK_REGION_FREE;
    region->id = 0;
    region->base = 0;
    region->size = 0;
    region->owner = 0;
    region->type = 0;
    region->flags = 0;
    region->reserved = 0;
    state->region_count = state->region_count - 1;
    if (base < state->next_physical) {
        state->next_physical = base;
    }
    state->allocation_epoch = state->allocation_epoch + 1;
    op->result = base;
    return NPK_REASON_NONE;
}

/* Milestone 5 payload services. These are internal kernel calls, not typed
 * operations: the object manager invokes them from its own COMMIT stage. */
int npk_memory_region_owned_by(npk_u64 region_id, npk_u64 owner)
{
    npk_u64 index = npk_find_region(region_id);
    struct NpkMemoryRegion *region;

    if (index == NPK_NO_INDEX) {
        return 0;
    }
    region = npk_region(index);
    if (region->state != NPK_REGION_ALLOCATED || region->owner != owner) {
        return 0;
    }
    return 1;
}

int npk_memory_address_space_owned_by(npk_u64 index, npk_u64 owner)
{
    struct NpkMemoryState *state = npk_state();
    struct NpkAddressSpace *space;

    if (index >= state->address_space_slots) {
        return 0;
    }
    space = npk_address_space(index);
    if (space->state != NPK_ADDRESS_SPACE_ACTIVE || space->owner != owner) {
        return 0;
    }
    return 1;
}

int npk_memory_release_for_object(npk_u64 region_id, npk_u64 owner)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 index = npk_find_region(region_id);
    struct NpkMemoryRegion *region;
    npk_u64 base;

    if (index == NPK_NO_INDEX) {
        return NPK_REASON_MEMORY_BAD_REGION;
    }
    region = npk_region(index);
    if (region->state != NPK_REGION_ALLOCATED) {
        return NPK_REASON_MEMORY_STATE;
    }
    if (region->owner != owner) {
        return NPK_REASON_MEMORY_OWNER;
    }
    if (region->reserved != 0) {
        return NPK_REASON_MEMORY_STATE;
    }
    base = region->base;
    region->state = NPK_REGION_FREE;
    region->id = 0;
    region->base = 0;
    region->size = 0;
    region->owner = 0;
    region->type = 0;
    region->flags = 0;
    region->reserved = 0;
    state->region_count = state->region_count - 1;
    if (base < state->next_physical) {
        state->next_physical = base;
    }
    state->allocation_epoch = state->allocation_epoch + 1;
    return 0;
}

static npk_u64 npk_commit_map(struct NpkOperation *op)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 asid = npk_address_space_id(op->memory_aux);
    struct NpkAddressSpace *space = npk_address_space(asid);
    npk_u64 root;
    npk_u64 new_page;
    npk_u64 pte_address;
    npk_u64 pte;
    npk_u64 level;
    npk_u64 reason;
    npk_u64 region_index;
    struct NpkMemoryRegion *region;

    if (space->root == 0) {
        reason = npk_raw_allocate_page(&root);
        if (reason != NPK_REASON_NONE) {
            return reason;
        }
        space->root = root;
        space->state = NPK_ADDRESS_SPACE_ACTIVE;
        space->owner = npk_request()->owner;
        space->mapping_count = 0;
    } else {
        root = space->root;
    }

    for (level = 0; level < 3; level = level + 1) {
        pte_address = npk_table_address(root,
            npk_page_table_index(op->memory_address, level));
        pte = *((npk_u64 *)pte_address);
        if ((pte & 0xE) != 0) {
            return NPK_REASON_MEMORY_PTE_CONFLICT;
        }
        if ((pte & NPK_PTE_VALID) == 0) {
            reason = npk_raw_allocate_page(&new_page);
            if (reason != NPK_REASON_NONE) {
                return reason;
            }
            pte = new_page | NPK_PTE_VALID;
            *((npk_u64 *)pte_address) = pte;
        }
        root = pte & NPK_PTE_PPN_MASK;
    }

    pte_address = npk_table_address(root,
        npk_page_table_index(op->memory_address, 3));
    pte = *((npk_u64 *)pte_address);
    if ((pte & NPK_PTE_VALID) != 0) {
        return NPK_REASON_MEMORY_PTE_CONFLICT;
    }
    pte = (op->memory_evidence & NPK_PTE_PPN_MASK) | op->memory_flags;
    *((npk_u64 *)pte_address) = pte;
    region_index = npk_find_region(npk_region_id_from_aux(op->memory_aux));
    if (region_index != NPK_NO_INDEX) {
        region = npk_region(region_index);
        region->reserved = region->reserved + 1;
    }
    space->mapping_count = space->mapping_count + 1;
    state->mapping_count = state->mapping_count + 1;
    state->invalidation_epoch = state->invalidation_epoch + 1;
    npk_memory_flush_tlb();
    op->result = op->memory_address;
    return NPK_REASON_NONE;
}

static npk_u64 npk_commit_unmap(struct NpkOperation *op)
{
    struct NpkMemoryState *state = npk_state();
    npk_u64 asid = npk_address_space_id(op->memory_aux);
    struct NpkAddressSpace *space = npk_address_space(asid);
    npk_u64 region_index;
    struct NpkMemoryRegion *region;

    if (*((npk_u64 *)op->memory_evidence) != op->memory_scratch) {
        return NPK_REASON_MEMORY_STATE;
    }
    *((npk_u64 *)op->memory_evidence) = 0;
    region_index = npk_find_region_by_base(op->memory_scratch & NPK_PTE_PPN_MASK);
    if (region_index != NPK_NO_INDEX) {
        region = npk_region(region_index);
        if (region->reserved > 0) {
            region->reserved = region->reserved - 1;
        }
    }
    if (space->mapping_count > 0) {
        space->mapping_count = space->mapping_count - 1;
    }
    if (state->mapping_count > 0) {
        state->mapping_count = state->mapping_count - 1;
    }
    state->invalidation_epoch = state->invalidation_epoch + 1;
    npk_memory_flush_tlb();
    op->result = op->memory_address;
    return NPK_REASON_NONE;
}

npk_u64 npk_memory_commit(struct NpkOperation *op)
{
    if (op->provisional == 0) {
        return NPK_REASON_MEMORY_STATE;
    }
    if (op->type == NPK_OP_MEMORY_ALLOCATE) {
        return npk_commit_allocate(op);
    }
    if (op->type == NPK_OP_MEMORY_RELEASE) {
        return npk_commit_release(op);
    }
    if (op->type == NPK_OP_MEMORY_QUERY) {
        op->result = op->provisional;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_MEMORY_MAP) {
        return npk_commit_map(op);
    }
    if (op->type == NPK_OP_MEMORY_UNMAP) {
        return npk_commit_unmap(op);
    }
    return NPK_REASON_MEMORY_UNSUPPORTED;
}

static npk_u64 npk_stage_status(npk_u64 status)
{
    if (status == NPK_REASON_MEMORY_BAD_REQUEST ||
            status == NPK_REASON_MEMORY_BAD_REGION ||
            status == NPK_REASON_MEMORY_BAD_SIZE ||
            status == NPK_REASON_MEMORY_BAD_ALIGN ||
            status == NPK_REASON_MEMORY_BAD_ADDRESS_SPACE) {
        return NPK_REASON_MEMORY_BAD_REQUEST;
    }
    return status;
}

int npk_memory_request(void)
{
    struct NpkMemoryState *state = npk_state();
    struct NpkMemoryRequest *request = npk_request();
    struct NpkMemoryResponse *response = npk_response();
    npk_u64 slot = NPK_NO_INDEX;
    npk_u64 type;
    npk_u64 reason = NPK_REASON_NONE;
    int status;
    int outcome = NPK_OUTCOME_PENDING;
    int i;

    for (i = 0; i < 8; i = i + 1) {
        *((npk_u64 *)response + i) = 0;
    }
    response->magic = NPK_MEMORY_RESPONSE_MAGIC;
    response->operation = request->operation;
    state->last_status = 0;
    state->last_reason = NPK_REASON_NONE;

    if (request->magic != NPK_MEMORY_REQUEST_MAGIC || request->id == 0 ||
            request->owner == 0) {
        state->last_status = NPK_REQUEST_BAD_REQUEST;
        response->status = NPK_REQUEST_BAD_REQUEST;
        return NPK_REQUEST_BAD_REQUEST;
    }
    type = request->operation;
    if (type < NPK_OP_MEMORY_ALLOCATE || type > NPK_OP_MEMORY_QUERY) {
        state->last_status = NPK_REQUEST_BAD_OP;
        response->status = NPK_REQUEST_BAD_OP;
        return NPK_REQUEST_BAD_OP;
    }

    status = npk_core_admit(type, request->id, NPK_MEMORY_REQUEST_ADDRESS, &slot);
    if (status != 0) {
        state->last_status = NPK_REQUEST_ADMIT_FAIL;
        state->last_reason = npk_stage_status(status);
        response->status = NPK_REQUEST_ADMIT_FAIL;
        response->reason = state->last_reason;
        return NPK_REQUEST_ADMIT_FAIL;
    }
    npk_core_set_memory_arguments(slot, request->address, request->size,
                                  request->flags, request->aux);
    for (i = 0; i < 5; i = i + 1) {
        outcome = npk_core_operation_outcome(slot);
        if (outcome != NPK_OUTCOME_PENDING) {
            break;
        }
        status = npk_core_advance(slot);
        if (status != 0) {
            state->last_status = NPK_REQUEST_ADVANCE_FAIL;
            break;
        }
    }

    reason = npk_core_operation_reason(slot);
    response->result = npk_core_operation_result(slot);
    response->aux = npk_core_operation_receipt_index(slot);
    response->receipt_index = npk_core_operation_receipt_index(slot);
    response->slot = slot;
    response->reason = reason;
    state->last_reason = reason;

    if (state->last_status == 0 && status != 0) {
        state->last_status = NPK_REQUEST_ADVANCE_FAIL;
    }
    if (state->last_status == 0) {
        if (outcome == NPK_OUTCOME_REJECTED) {
            state->last_status = NPK_REQUEST_REJECTED;
        } else if (outcome == NPK_OUTCOME_FAILED) {
            state->last_status = NPK_REQUEST_FAILED;
        } else if (outcome != NPK_OUTCOME_COMPLETE) {
            state->last_status = NPK_REQUEST_BAD_OUTCOME;
        }
    }
    response->status = state->last_status;
    if (outcome == NPK_OUTCOME_COMPLETE || outcome == NPK_OUTCOME_REJECTED ||
            outcome == NPK_OUTCOME_FAILED) {
        npk_core_release(slot);
    }
    return (int)state->last_status;
}
