#ifndef NPK_CORE_H
#define NPK_CORE_H

typedef unsigned long npk_u64;

#define NPK_NO_INDEX 0xFFFFFFFFFFFFFFFF

/* Shared with the trap handlers; the core owns all writes to these fields. */
struct NpkOperation {
    npk_u64 id;
    npk_u64 type;
    npk_u64 stage;
    npk_u64 state;
    npk_u64 pu;
    npk_u64 input;
    npk_u64 provisional;
    npk_u64 result;
    npk_u64 reason;
    npk_u64 outcome;
    npk_u64 receipt_index;
    npk_u64 receipt_base;
};

#endif
