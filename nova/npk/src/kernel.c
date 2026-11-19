/*
 * Minimal NPK C entry point for the boot foundation milestone.
 *
 * The assembly entry point owns all architectural setup. `npk_main` is the
 * first processing coordinator: it submits the boot-verification operation to
 * the NPK pipeline and returns success only if that operation committed and
 * completed. Later milestones expand initialization in an explicit, tested
 * order rather than advertising subsystems that do not exist yet.
 */

int npk_process_boot(void);

int npk_main(void)
{
    return npk_process_boot();
}
