/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "cc-mertensia-accounts.h"

gboolean
cc_mertensia_accounts_matches (const char *panel_id, GVariant *parameters)
{
    g_autoptr(GVariant) page = NULL;

    /* Also accept the legacy panel name used by older desktop links. */
    if (g_strcmp0 (panel_id, "users") == 0 || g_strcmp0 (panel_id, "user-accounts") == 0)
        return TRUE;

    if (g_strcmp0 (panel_id, "system") != 0 || parameters == NULL ||
        !g_variant_is_of_type (parameters, G_VARIANT_TYPE ("av")) ||
        g_variant_n_children (parameters) == 0)
        return FALSE;

    g_variant_get_child (parameters, 0, "v", &page);
    return g_variant_is_of_type (page, G_VARIANT_TYPE_STRING) &&
           g_str_equal (g_variant_get_string (page, NULL), "users");
}

gboolean
cc_mertensia_accounts_launch (GError **error)
{
    char *argv[] = { "/usr/bin/mertensia-accounts", NULL };

    /* Run as the session user. The Accounts helper owns authorization. */
    return g_spawn_async (NULL, argv, NULL, G_SPAWN_DEFAULT, NULL, NULL, NULL, error);
}

void
cc_mertensia_accounts_open (GtkWidget *parent)
{
    g_autoptr(GError) error = NULL;

    if (!cc_mertensia_accounts_launch (&error)) {
        AdwDialog *dialog = adw_alert_dialog_new ("Could not open Mertensia Accounts", error->message);
        adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "close", "_Close");
        adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "close");
        adw_dialog_present (dialog, parent);
    }
}
