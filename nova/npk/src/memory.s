; NPK Milestone 4 translation-invalidation helper.
;
; Nova exposes a full TLB invalidate as TLBI r0. There is no ASID and no
; shootdown model in Nova ISA v1.0, so NPK uses the only invalidation the
; platform really provides. The C memory subsystem calls this helper after it
; publishes or removes a leaf PTE.

.section .text
.global npk_memory_flush_tlb
npk_memory_flush_tlb:
    TLBI r0
    RET
