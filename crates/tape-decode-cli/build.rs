//! Embeds the Windows application icon into the executable.
//!
//! A Rust binary has no icon unless one is compiled into its resources, so this
//! runs only on Windows and is a no-op everywhere else.

fn main() {
    #[cfg(windows)]
    {
        let icon = std::path::Path::new("../../resources/icon/tape-decode-full.ico");
        println!("cargo:rerun-if-changed=../../resources/icon/tape-decode-full.ico");
        if icon.is_file() {
            let mut res = winresource::WindowsResource::new();
            res.set_icon(icon.to_str().expect("icon path is not valid UTF-8"));
            if let Err(error) = res.compile() {
                // An icon is cosmetic; failing to attach one should not stop a
                // build that would otherwise succeed.
                println!("cargo:warning=could not embed the icon: {error}");
            }
        }
    }
}
