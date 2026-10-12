// Build config for the compiled stylesheet. Tokens come from assets/tokens.js (the Stitch design system).
const fs = require("fs");
const src = fs.readFileSync(__dirname + "/../assets/tokens.js", "utf8");
const tailwind = {};
new Function("tailwind", src)(tailwind);
module.exports = { ...tailwind.config, content: [__dirname + "/../*.html", __dirname + "/../assets/*.js"] };
