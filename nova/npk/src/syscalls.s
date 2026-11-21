; NPK Milestone 6 syscall time helper.
;
; Nova exposes the time CSR to user code, but C has no inline-assembly facility
; in the freestanding Phase 1 compiler subset. This tiny assembly leaf gives the
; syscall subsystem the live CSR value without changing the ISA or the runtime
; ABI. It returns in r7, the nova-cc C return register; the syscall service then
; publishes that value to the user-visible a0/r1 slot in the saved trap frame.

.section .text
.global npk_syscall_read_time
npk_syscall_read_time:
    CSRR r7, time
    RET
