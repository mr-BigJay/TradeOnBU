package ir.tradeonbu.probe;
public final class ChannelFilterTest {
    static void check(boolean condition) { if (!condition) throw new AssertionError(); }
    public static void main(String[] args) {
        check(ChannelFilter.matches("BehDark (VIP)", "\u200fBehDark\u00a0(VIP)"));
        check(ChannelFilter.matches("BehDark (VIP)", "Other", "behdark (vip)"));
        check(!ChannelFilter.matches("", ""));
        check(!ChannelFilter.matches("BehDark (VIP)", "BehDark (VIP) scam"));
        check(!ChannelFilter.matches("BehDark (VIP)", "Other channel"));
        check(ChannelFilter.allowedPackage("org.telegram.messenger"));
        check(!ChannelFilter.allowedPackage("com.example.fake.telegram"));
        check(!ChannelFilter.allowedPackage(null));
        System.out.println("8 channel/package filter checks passed");
    }
}
