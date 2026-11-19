; NPK Milestone 3 machine-mode exception vector.
;
; Nova exception entry preserves GPRs but does not build a software trap frame.
; This stub saves all 32 registers plus the guest-visible exception state at a
; fixed frame address, switches to a dedicated trap stack, calls the C dispatch,
; restores the interrupted context, and returns with MRET. The internal four-deep
; Nova save stack remains authoritative for the privilege/PC restored by MRET.
;
; The frame is single-level: a trap taken while this stub is active will reuse
; it. Nested exception handling is not supported and is not claimed.

.section .text
.global _npk_exception_vector
_npk_exception_vector:
    ; Preserve the two scratch registers before clobbering them. Low scratch
    ; slots are reached through r0 so no other register is needed.
    STD r15, [r0 + 0x208]
    STD r16, [r0 + 0x200]
    LI r16, 1
    STD r16, [r0 + 0x210]

    ; Establish the fixed trap-frame pointer and copy the saved scratch values
    ; into their architectural slots.
    LI r15, 0x90900
    LDD r16, [r0 + 0x208]
    STD r16, [r15 + 120]
    LDD r16, [r0 + 0x200]
    STD r16, [r15 + 128]

    ; Save the remaining general-purpose registers.
    STD r0, [r15 + 0]
    STD r1, [r15 + 8]
    STD r2, [r15 + 16]
    STD r3, [r15 + 24]
    STD r4, [r15 + 32]
    STD r5, [r15 + 40]
    STD r6, [r15 + 48]
    STD r7, [r15 + 56]
    STD r8, [r15 + 64]
    STD r9, [r15 + 72]
    STD r10, [r15 + 80]
    STD r11, [r15 + 88]
    STD r12, [r15 + 96]
    STD r13, [r15 + 104]
    STD r14, [r15 + 112]
    STD r17, [r15 + 136]
    STD r18, [r15 + 144]
    STD r19, [r15 + 152]
    STD r20, [r15 + 160]
    STD r21, [r15 + 168]
    STD r22, [r15 + 176]
    STD r23, [r15 + 184]
    STD r24, [r15 + 192]
    STD r25, [r15 + 200]
    STD r26, [r15 + 208]
    STD r27, [r15 + 216]
    STD r28, [r15 + 224]
    STD r29, [r15 + 232]
    STD r30, [r15 + 240]
    STD r31, [r15 + 248]

    ; Save guest-visible exception state. The privilege word recorded here is
    ; the handler privilege; the pre-trap privilege lives in the Nova save stack
    ; and is restored by MRET.
    CSRR r16, psr
    STD r16, [r15 + 256]
    CSRR r16, epc
    STD r16, [r15 + 264]
    CSRR r16, ecause
    STD r16, [r15 + 272]
    CSRR r16, ecode
    STD r16, [r15 + 280]
    CSRR r16, priv
    STD r16, [r15 + 288]
    CSRR r16, tval
    STD r16, [r15 + 296]

    ; Run the C dispatcher on a separate stack. a0 carries the frame address.
    LI sp, 0x88000
    LI a0, 0x90900
    LA ra, .Lnpk_trap_return
    BL npk_trap_dispatch
.Lnpk_trap_return:

    ; Restore guest state. r15 remains the frame pointer until the final load.
    LI r15, 0x90900
    LDD r16, [r15 + 296]
    CSRW tval, r16
    LDD r16, [r15 + 256]
    CSRW psr, r16
    LDD r1, [r15 + 8]
    LDD r2, [r15 + 16]
    LDD r3, [r15 + 24]
    LDD r4, [r15 + 32]
    LDD r5, [r15 + 40]
    LDD r6, [r15 + 48]
    LDD r7, [r15 + 56]
    LDD r8, [r15 + 64]
    LDD r9, [r15 + 72]
    LDD r10, [r15 + 80]
    LDD r11, [r15 + 88]
    LDD r12, [r15 + 96]
    LDD r13, [r15 + 104]
    LDD r14, [r15 + 112]
    LDD r16, [r15 + 128]
    LDD r17, [r15 + 136]
    LDD r18, [r15 + 144]
    LDD r19, [r15 + 152]
    LDD r20, [r15 + 160]
    LDD r21, [r15 + 168]
    LDD r22, [r15 + 176]
    LDD r23, [r15 + 184]
    LDD r24, [r15 + 192]
    LDD r25, [r15 + 200]
    LDD r26, [r15 + 208]
    LDD r27, [r15 + 216]
    LDD r28, [r15 + 224]
    LDD r29, [r15 + 232]
    LDD r30, [r15 + 240]
    LDD r31, [r15 + 248]
    STD r0, [r0 + 0x210]
    LDD r15, [r15 + 120]
    MRET
