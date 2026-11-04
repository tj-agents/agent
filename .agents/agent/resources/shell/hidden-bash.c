// Claude's executable-only Bash setting needs an adapter to tj_hook's command interface.
#include <windows.h>
#include <wchar.h>
#include <stdlib.h>
#include <string.h>

static int fail(const char *message) {
    HANDLE error = GetStdHandle(STD_ERROR_HANDLE);
    DWORD written;
    if (error && error != INVALID_HANDLE_VALUE) {
        WriteFile(error, message, (DWORD)strlen(message), &written, NULL);
    }
    return 127;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR ignored, int show) {
    (void)instance;
    (void)previous;
    (void)ignored;
    (void)show;

    DWORD length = GetEnvironmentVariableW(L"TJ_AGENTS_REAL_GIT_BASH", NULL, 0);
    if (length < 2) return fail("hidden-bash: TJ_AGENTS_REAL_GIT_BASH is not set\n");
    wchar_t *bash = (wchar_t *)calloc(length, sizeof(wchar_t));
    if (!bash) return fail("hidden-bash: allocation failed\n");
    if (!GetEnvironmentVariableW(L"TJ_AGENTS_REAL_GIT_BASH", bash, length)) {
        free(bash);
        return fail("hidden-bash: real Git Bash path is unavailable\n");
    }

    const wchar_t *tail = GetCommandLineW();
    if (*tail == L'"') {
        ++tail;
        while (*tail && *tail != L'"') ++tail;
        if (*tail) ++tail;
    } else {
        while (*tail && *tail != L' ' && *tail != L'\t') ++tail;
    }
    while (*tail == L' ' || *tail == L'\t') ++tail;

    DWORD path_length = GetEnvironmentVariableW(L"PATH", NULL, 0);
    wchar_t *search = path_length ? (wchar_t *)calloc(path_length, sizeof(wchar_t)) : NULL;
    if (!search || !GetEnvironmentVariableW(L"PATH", search, path_length)) {
        free(search);
        free(bash);
        return fail("hidden-bash: PATH is unavailable\n");
    }
    DWORD hook_length = SearchPathW(search, L"tj_hook.exe", NULL, 0, NULL, NULL);
    if (!hook_length) {
        free(search);
        free(bash);
        return fail("hidden-bash: tj_hook is not installed on PATH\n");
    }
    wchar_t *hook = (wchar_t *)calloc((size_t)hook_length + 1, sizeof(wchar_t));
    if (!hook || !SearchPathW(search, L"tj_hook.exe", NULL, hook_length + 1, hook, NULL)) {
        free(search);
        free(hook);
        free(bash);
        return fail("hidden-bash: tj_hook path is unavailable\n");
    }
    free(search);
    const wchar_t *separator = L"\" --preserve-descendants -- \"";
    size_t command_length = wcslen(hook) + wcslen(bash) + wcslen(tail) + wcslen(separator) + 4;
    wchar_t *command = (wchar_t *)calloc(command_length, sizeof(wchar_t));
    if (!command) {
        free(hook);
        free(bash);
        return fail("hidden-bash: allocation failed\n");
    }
    wcscat(command, L"\"");
    wcscat(command, hook);
    wcscat(command, separator);
    wcscat(command, bash);
    wcscat(command, L"\"");
    if (*tail) {
        wcscat(command, L" ");
        wcscat(command, tail);
    }

    STARTUPINFOW startup = {0};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    PROCESS_INFORMATION process = {0};
    BOOL started = CreateProcessW(hook, command, NULL, NULL, TRUE,
                                  0, NULL, NULL, &startup, &process);
    free(command);
    free(hook);
    free(bash);
    if (!started) return fail("hidden-bash: failed to start tj_hook\n");
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = 127;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return (int)code;
}
