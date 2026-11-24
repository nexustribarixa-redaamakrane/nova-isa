; NPK Milestone 8 task trampoline helpers.
;
; Nova MRET restores EPC from the hidden exception stack, so NPK cannot redirect
; return by editing frame.epc alone. The cooperative scheduler patches a
; suspended task's writable return site with JMP r15 and restores r15 with the
; selected task resume PC before trap.s executes MRET.

.section .text
.global npk_task_trampoline_word
.global npk_task_trampoline_jmp_r15
.global npk_task_halt

npk_task_trampoline_jmp_r15:
    JMP r15

; Return the 4-byte JMP r15 encoding in a0 (zero-extended).
npk_task_trampoline_word:
    LI a0, 0x50780000
    RET

; Cooperative idle: no runnable task remains.
npk_task_halt:
    HALT

; Return halt PC for C scheduler.
.global npk_task_halt_pc
npk_task_halt_pc:
    LA a0, npk_task_halt
    RET