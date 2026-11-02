/* Test-only home/keyring stand-ins; never installed in the OS. */
#include <security/pam_modules.h>
#include <stdio.h>
#include <string.h>

PAM_EXTERN int pam_sm_chauthtok(pam_handle_t *pamh, int flags,
                               int argc, const char **argv) {
    const void *old = NULL, *next = NULL;
    if (flags & PAM_PRELIM_CHECK)
        return PAM_SUCCESS;
    if (argc != 2 || pam_get_item(pamh, PAM_OLDAUTHTOK, &old) != PAM_SUCCESS ||
        pam_get_item(pamh, PAM_AUTHTOK, &next) != PAM_SUCCESS)
        return PAM_SERVICE_ERR;
    if (!old || strcmp(old, "old λ password") != 0)
        return PAM_AUTH_ERR;
    if (!next || strcmp(next, "new λ password") != 0)
        return PAM_AUTHTOK_ERR;
    FILE *file = fopen(argv[1], "a");
    if (!file)
        return PAM_SYSTEM_ERR;
    fprintf(file, "%s\n", argv[0]);
    fclose(file);
    if (strcmp(argv[0], "ignored-home") == 0)
        return PAM_IGNORE;
    return strcmp(argv[0], "failed-home") == 0 ? PAM_AUTHTOK_ERR : PAM_SUCCESS;
}
