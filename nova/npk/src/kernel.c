/*
 * Minimal NPK C entry point.
 *
 * The assembly entry point owns all architectural setup. `npk_main` runs the
 * boot-processing pipeline, initializes the Milestone 3 trap observation state,
 * and returns success only when boot processing committed and completed. Trap
 * delivery itself is driven by the real Nova vector in `trap.s`; no allocator,
 * syscall service, device driver, scheduler, or user loader exists yet.
 */

int npk_process_boot(void);
int npk_trap_init(void);

int npk_main(void)
{
    int status = npk_process_boot();

    if (status != 0) {
        return status;
    }
    return npk_trap_init();
}
