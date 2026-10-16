import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.time.LocalTime;

/**
 * A stand-in for a Paper server that behaves like the real thing from the
 * outside: it writes logs/latest.log with Paper's own boot and readiness
 * markers, reads its console from stdin, saves and exits on `stop`, and can be
 * killed or made to die to exercise crash recovery.
 *
 * It exists so the lifecycle manager can be verified against a real JVM (real
 * argv, real process detection, real signals) without a 50 MB download.
 */
public final class FakePaper {

    private static Path logPath;

    private static void log(String message) {
        String line = "[" + LocalTime.now().withNano(0) + " INFO]: " + message;
        try {
            Files.write(logPath, (line + "\n").getBytes(StandardCharsets.UTF_8),
                        StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException ignored) {
            // best effort, exactly like a log write under disk pressure
        }
        System.out.println(line);
        System.out.flush();
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }

    private static long envLong(String name, long fallback) {
        String raw = System.getenv(name);
        if (raw == null || raw.trim().isEmpty()) {
            return fallback;
        }
        try {
            return Long.parseLong(raw.trim());
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    private static boolean envBool(String name, boolean fallback) {
        String raw = System.getenv(name);
        if (raw == null) {
            return fallback;
        }
        return raw.trim().equalsIgnoreCase("true") || raw.trim().equals("1");
    }

    public static void main(String[] args) throws Exception {
        Path serverDir = Paths.get("").toAbsolutePath();
        Path logsDir = serverDir.resolve("logs");
        Files.createDirectories(logsDir);
        logPath = logsDir.resolve("latest.log");
        if (envBool("FAKE_TRUNCATE_LOG", true)) {
            Files.write(logPath, new byte[0]);
        }

        long readyAfter = envLong("FAKE_READY_AFTER_MS", 1500L);
        long exitAfter = envLong("FAKE_EXIT_AFTER_MS", 0L);
        long exitCode = envLong("FAKE_EXIT_CODE", 3L);
        String bootFail = System.getenv("FAKE_BOOT_FAIL");
        boolean ignoreStdin = envBool("FAKE_IGNORE_STDIN", false);
        boolean ignoreSigterm = envBool("FAKE_IGNORE_SIGTERM", false);

        if (ignoreSigterm) {
            // Make the SIGTERM escalation path reachable, the way a wedged JVM
            // that installs its own handler would behave.
            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                while (true) {
                    try {
                        Thread.sleep(50);
                    } catch (InterruptedException e) {
                        return;
                    }
                }
            }));
        }

        log("Starting minecraft server version 1.21.4");
        log("Loading properties");
        log("This server is running Paper version 1.21.4-232-master@2658b74 "
            + "(2025-02-14T14:22:22Z) (Implementing API version 1.21.4-R0.1-SNAPSHOT)");

        // Real Paper generates/opens the world here. The lifecycle refuses to
        // start when a world is expected but level.dat is gone (so a lost world
        // is never silently regenerated), so the stand-in must create one too.
        Path worldDir = serverDir.resolve("world");
        Files.createDirectories(worldDir.resolve("region"));
        Path levelDat = worldDir.resolve("level.dat");
        if (!Files.exists(levelDat)) {
            Files.write(levelDat, "fake-level-data".getBytes(StandardCharsets.UTF_8));
        }
        log("Preparing level \"world\"");

        if ("eula".equals(bootFail)) {
            log("You need to agree to the EULA in order to run the server. "
                + "Go to https://aka.ms/MinecraftEULA");
            System.out.flush();
            System.exit(1);
        }
        if ("class".equals(bootFail)) {
            log("Error: Could not find or load main class io.papermc.paperclip.Main");
            System.out.flush();
            System.exit(1);
        }

        Thread reader = new Thread(() -> {
            if (ignoreStdin) {
                return;
            }
            try (BufferedReader br = new BufferedReader(
                    new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
                String line;
                while ((line = br.readLine()) != null) {
                    String cmd = line.trim();
                    if (cmd.equals("stop")) {
                        log("Stopping the server");
                        log("Saving worlds");
                        log("Saving chunks for level 'ServerLevel[world]'/minecraft:overworld");
                        log("ThreadedAnvilChunkStorage (world): All chunks are saved");
                        System.out.flush();
                        new Thread(() -> {
                            sleep(300);
                            System.out.flush();
                            System.exit(0);
                        }).start();
                    } else if (cmd.equals("save-all")) {
                        log("Saving the game (this may take a moment!)");
                        log("Saved the game");
                    } else if (cmd.equals("list")) {
                        log("There are 0 of a max of 20 players online:");
                    } else if (!cmd.isEmpty()) {
                        log("Unknown or incomplete command, see below for error");
                    }
                }
            } catch (IOException ignored) {
            }
        }, "Server console handler");
        reader.setDaemon(true);
        reader.start();

        sleep(readyAfter);
        log("Preparing spawn area: 100%");
        log(String.format("Done (%.3fs)! For help, type \"help\"",
                          (readyAfter / 1000.0) + 3.0));
        log("Timings Reset");

        if (exitAfter > 0) {
            sleep(exitAfter);
            System.out.flush();
            System.exit((int) exitCode);
        }

        while (true) {
            sleep(1000);
        }
    }
}
