; NPK Milestone 9 VM-entry helper.
;
; Nova ISA v1.0 has no "enter user process" instruction. The honest kernel ->
; user transition available to NPK is the documented VMLAUNCH path: machine
; software programs vpc/vpriv/vptbr, VMLAUNCH saves the host context, and a
; guest trap returns to the host through the architectural VM-exit path. This
; leaf preserves the nova-cc callee-saved registers, installs a deterministic
; guest register file, records the observed exit cause, and returns zero in
; r1/a0 on a successful host resume (the kernel's reason-code convention).
;
; The QEMU boot verifier does not execute this leaf, so its existing instruction
; coverage contract is unchanged.

.section .text
.global npk_exec_vmlaunch
npk_exec_vmlaunch:
    ; Save host callee-saved state, including ra, before the guest can change
    ; any GPR. r1/r29 are caller-saved and are intentionally replaced by the
    ; guest argument/stack.
    LI r7, 0xB4880
    STD r11, [r7]
    STD r12, [r7 + 8]
    STD r13, [r7 + 16]
    STD r14, [r7 + 24]
    STD r28, [r7 + 32]
    STD r29, [r7 + 40]
    STD r30, [r7 + 48]
    STD r31, [r7 + 56]

    ; Program guest control state from the fixed launch context.
    LI r7, 0xB4800
    LDD r5, [r7 + 16]
    CSRW vpc, r5
    LDD r5, [r7 + 24]
    CSRW vpriv, r5
    LDD r5, [r7 + 32]
    CSRW vptbr, r5
    CSRR r5, priv
    STD r5, [r7 + 104]
    CSRR r5, ptbr
    STD r5, [r7 + 112]
    LDD r29, [r7 + 40]
    LDD r1, [r7 + 48]

    ; Deterministic guest registers. r1 and r29 are the user argument/stack;
    ; r31 is the guest return address and starts at zero.
    LI r2, 0
    LI r3, 0
    LI r4, 0
    LI r5, 0
    LI r6, 0
    LI r7, 0
    LI r8, 0
    LI r9, 0
    LI r10, 0
    LI r15, 0
    LI r16, 0
    LI r17, 0
    LI r18, 0
    LI r19, 0
    LI r20, 0
    LI r21, 0
    LI r22, 0
    LI r23, 0
    LI r24, 0
    LI r25, 0
    LI r26, 0
    LI r27, 0
    LI r28, 0
    LI r31, 0
    VMLAUNCH

    ; The guest has exited. r1 still holds its a0 value; publish it and the
    ; architectural exit CSRs before restoring host registers.
    LI r6, 0xB4800
    STD r1, [r6 + 64]
    CSRR r5, vexit
    STD r5, [r6 + 72]
    CSRR r5, ecause
    STD r5, [r6 + 80]
    LI r5, 1
    STD r5, [r6]

    LI r7, 0xB4880
    LDD r11, [r7]
    LDD r12, [r7 + 8]
    LDD r13, [r7 + 16]
    LDD r14, [r7 + 24]
    LDD r28, [r7 + 32]
    LDD r29, [r7 + 40]
    LDD r30, [r7 + 48]
    LDD r31, [r7 + 56]
    LI r7, 0
    LI r1, 0
    LI r5, 1
    STD r5, [r6 + 120]
    RET
