/* CI-only native PAM fixture. Never installed by the greetd recipe. */
#define _GNU_SOURCE
#include <security/pam_appl.h>
#include <security/pam_modules.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int question(pam_handle_t *pamh, int style, const char *text,
                    const char *expected) {
    const void *value = NULL;
    if (pam_get_item(pamh, PAM_CONV, &value) != PAM_SUCCESS || !value)
        return PAM_CONV_ERR;
    const struct pam_conv *conv = value;
    const struct pam_message message = {style, text};
    const struct pam_message *messages[] = {&message};
    struct pam_response *response = NULL;
    int result = conv->conv(1, messages, &response, conv->appdata_ptr);
    if (result != PAM_SUCCESS || !response) return PAM_CONV_ERR;
    int matches = expected ? response[0].resp && !strcmp(response[0].resp, expected)
                           : response[0].resp == NULL;
    if (response[0].resp) {
        volatile char *cursor = response[0].resp;
        size_t bytes = strlen(response[0].resp);
        while (bytes--) *cursor++ = 0;
        free(response[0].resp);
    }
    free(response);
    return matches ? PAM_SUCCESS : PAM_AUTH_ERR;
}

static int phase(const char *path, const char *name) {
    char writing[4096];
    int path_size = snprintf(writing, sizeof(writing), "%s.writing", path);
    if (path_size <= 0 || path_size >= (int)sizeof(writing)) return PAM_SYSTEM_ERR;
    int fd = open(writing, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) return PAM_SYSTEM_ERR;
    char receipt[256];
    int size = snprintf(receipt, sizeof(receipt),
                        "{\"schema\":1,\"phase\":\"%s\",\"pid\":%ld,\"parent\":%ld}\n",
                        name, (long)getpid(), (long)getppid());
    int result = PAM_SUCCESS;
    if (size <= 0 || size >= (int)sizeof(receipt) || write(fd, receipt, size) != size)
        result = PAM_SYSTEM_ERR;
    close(fd);
    /* Publish a complete bounded inode without replacing a preexisting phase.
       This is a visible-write barrier, not a storage durability claim. */
    if (result == PAM_SUCCESS && link(writing, path) != 0) result = PAM_SYSTEM_ERR;
    if (unlink(writing) != 0) result = PAM_SYSTEM_ERR;
    return result;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags,
                                  int argc, const char **argv) {
    (void)flags;
    if (argc != 3 || strcmp(argv[0], "owned-native-proof") ||
        (strcmp(argv[1], "multi") && strcmp(argv[1], "hung")))
        return PAM_SERVICE_ERR;
    char admitted[4096];
    int path_size = snprintf(admitted, sizeof(admitted), "%s.admitted", argv[2]);
    if (path_size <= 0 || path_size >= (int)sizeof(admitted) ||
        phase(admitted, "admitted") != PAM_SUCCESS) return PAM_SYSTEM_ERR;
    if (question(pamh, PAM_PROMPT_ECHO_OFF, "Password", "fixture-password") != PAM_SUCCESS)
        return PAM_AUTH_ERR;
    if (question(pamh, PAM_TEXT_INFO, "Literal <fixture> information", NULL) != PAM_SUCCESS)
        return PAM_AUTH_ERR;
    if (question(pamh, PAM_ERROR_MSG, "Literal & fixture warning", NULL) != PAM_SUCCESS)
        return PAM_AUTH_ERR;
    if (!strcmp(argv[1], "multi")) {
        if (question(pamh, PAM_PROMPT_ECHO_OFF, "One-time code", "fixture-otp") != PAM_SUCCESS)
            return PAM_AUTH_ERR;
        return question(pamh, PAM_PROMPT_ECHO_ON, "Visible confirmation", "fixture-visible");
    }
    /* This module deliberately cannot read greetd's cooperative Cancel message.
       Ignore TERM so the production held pidfd KILL/reaper path is exercised. */
    if (signal(SIGTERM, SIG_IGN) == SIG_ERR) return PAM_SYSTEM_ERR;
    if (phase(argv[2], "hung") != PAM_SUCCESS) return PAM_SYSTEM_ERR;
    for (;;) pause();
}
PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc, const char **argv) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}
