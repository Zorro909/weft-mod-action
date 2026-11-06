use weft_sdk::{declare_mod, Registry};

fn init(_registry: &mut Registry) {}

declare_mod!(deps: ["base"], init: init);
