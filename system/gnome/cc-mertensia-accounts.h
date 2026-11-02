/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once

#include <adwaita.h>

gboolean cc_mertensia_accounts_matches (const char *panel_id, GVariant *parameters);
gboolean cc_mertensia_accounts_launch (GError **error);
void cc_mertensia_accounts_open (GtkWidget *parent);
