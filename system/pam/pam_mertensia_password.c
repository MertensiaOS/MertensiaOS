/* SPDX-License-Identifier: MIT
 * Collect both tokens before pam_systemd_home changes the encrypted home.
 * homed does not necessarily populate PAM_OLDAUTHTOK when prompting itself;
 * pam_oo7 needs that token to re-encrypt the existing login keyring.
 * This module collects credentials only. The following requisite homed module
 * is responsible for authenticating the current password and changing it.
 */
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <security/pam_modules.h>
#include <security/pam_ext.h>

PAM_EXTERN int pam_sm_chauthtok(pam_handle_t *pamh, int flags,
                               int argc, const char **argv) {
    const char *token = NULL;
    char *confirmation = NULL;
    int result;
    (void) argc;
    (void) argv;

    if (flags & PAM_UPDATE_AUTHTOK)
        return PAM_SUCCESS;
    if (!(flags & PAM_PRELIM_CHECK))
        return PAM_SERVICE_ERR;

    result = pam_get_authtok(pamh, PAM_OLDAUTHTOK, &token, "Current password: ");
    if (result != PAM_SUCCESS)
        return result;
    if (!token || !*token)
        return PAM_AUTHTOK_ERR;

    result = pam_get_authtok_noverify(pamh, &token, "New password: ");
    if (result != PAM_SUCCESS)
        return result;
    if (!token || !*token)
        return PAM_AUTHTOK_ERR;
    /* pam_get_authtok_verify adds a translated prefix to its prompt. Use our
     * own fixed confirmation prompt so the GUI never guesses prompt meaning. */
    result = pam_prompt(pamh, PAM_PROMPT_ECHO_OFF, &confirmation, "Confirm new password: ");
    if (result == PAM_SUCCESS && (!confirmation || strcmp(token, confirmation) != 0))
        result = PAM_AUTHTOK_ERR;
    if (confirmation) {
        volatile char *cursor = confirmation;
        size_t length = strlen(confirmation);
        while (length--)
            *cursor++ = 0;
        free(confirmation);
    }
    return result;
}
