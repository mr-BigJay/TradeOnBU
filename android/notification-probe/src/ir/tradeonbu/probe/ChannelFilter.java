package ir.tradeonbu.probe;

import java.text.Normalizer;
import java.util.Locale;

/** Exact channel-title matching only; never match message body or substrings. */
public final class ChannelFilter {
    public static String normalize(String value) {
        if (value == null) return "";
        return Normalizer.normalize(value, Normalizer.Form.NFKC)
                .replaceAll("[\\p{Cf}]", "").replaceAll("[\\s\\p{Z}]+", " ").trim().toLowerCase(Locale.ROOT);
    }
    public static boolean allowedPackage(String value) {
        return "org.telegram.messenger".equals(value) || "org.telegram.messenger.web".equals(value)
                || "org.thunderdog.challegram".equals(value);
    }
    public static boolean matches(String expected, String... titles) {
        String wanted = normalize(expected);
        if (wanted.isEmpty()) return false;
        for (String title : titles) if (wanted.equals(normalize(title))) return true;
        return false;
    }
}
