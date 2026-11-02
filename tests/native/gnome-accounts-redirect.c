/* Exercise the actual downstream routing and launcher without a desktop. */
#include "cc-mertensia-accounts.h"

static gboolean fail_spawn;
static guint launches;

gboolean
__wrap_g_spawn_async (const gchar *working_directory, gchar **argv, gchar **envp,
                     GSpawnFlags flags, GSpawnChildSetupFunc child_setup,
                     gpointer user_data, GPid *child_pid, GError **error)
{
    g_assert_null (working_directory);
    g_assert_cmpstr (argv[0], ==, "/usr/bin/mertensia-accounts");
    g_assert_null (argv[1]);
    g_assert_null (envp);
    g_assert_cmpint (flags, ==, G_SPAWN_DEFAULT);
    g_assert_null (child_setup);
    g_assert_null (user_data);
    g_assert_null (child_pid);
    launches++;
    if (fail_spawn) {
        g_set_error_literal (error, G_SPAWN_ERROR, G_SPAWN_ERROR_NOENT, "Missing Accounts launcher");
        return FALSE;
    }
    return TRUE;
}

static void
test_routes (void)
{
    const struct { const char *panel; const char *parameters; gboolean redirect; } cases[] = {
        { "users", NULL, TRUE },
        { "user-accounts", NULL, TRUE },
        { "system", "[<'users'>]", TRUE },
        { "system", "[<'users'>, <'alice'>]", TRUE },
        { "system", NULL, FALSE },
        { "system", "@av []", FALSE },
        { "system", "[<'about'>]", FALSE },
        { "system", "[<'region'>]", FALSE },
        { "system", "[<42>]", FALSE },
        { "system", "'users'", FALSE },
        { "online-accounts", NULL, FALSE },
        { "online-accounts", "[<'users'>]", FALSE },
        { "privacy", "[<'users'>]", FALSE },
        { NULL, NULL, FALSE },
    };
    for (guint i = 0; i < G_N_ELEMENTS (cases); i++) {
        g_autoptr(GVariant) parameters = NULL;
        if (cases[i].parameters)
            parameters = g_variant_ref_sink (g_variant_parse (NULL, cases[i].parameters, NULL, NULL, NULL));
        g_assert_cmpint (cc_mertensia_accounts_matches (cases[i].panel, parameters), ==, cases[i].redirect);
    }
}

static void
test_launch (void)
{
    g_autoptr(GError) error = NULL;
    fail_spawn = FALSE;
    g_assert_true (cc_mertensia_accounts_launch (&error));
    g_assert_no_error (error);
    fail_spawn = TRUE;
    g_assert_false (cc_mertensia_accounts_launch (&error));
    g_assert_error (error, G_SPAWN_ERROR, G_SPAWN_ERROR_NOENT);
    g_assert_cmpuint (launches, ==, 2);
}

int
main (int argc, char **argv)
{
    g_test_init (&argc, &argv, NULL);
    g_test_add_func ("/accounts/routes", test_routes);
    g_test_add_func ("/accounts/launch", test_launch);
    return g_test_run ();
}
