/*
 * Minimal NPK C entry point.
 *
 * The assembly entry point owns all architectural setup. `npk_main` runs the
 * boot-processing pipeline, initializes the Milestone 3 trap observation state,
 * initializes the Milestone 4 memory subsystem, and returns success only when
 * every coordinator stage succeeds. Trap delivery is driven by the real Nova
 * vector in `trap.s`; memory work is driven by the typed processing core from
 * `memory.c`. There is still no syscall service, device driver, scheduler, or
 * user loader.
 */

int npk_process_boot(void);
int npk_trap_init(void);
int npk_memory_init(void);

int npk_main(void)
{
    int status = npk_process_boot();

    if (status != 0) {
        return status;
    }
    status = npk_trap_init();
    if (status != 0) {
        return status;
    }
    return npk_memory_init();
}
