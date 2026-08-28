import AxeBuilder from "@axe-core/playwright";

export async function assertWcag22AA(page, label) {
  const results = await new AxeBuilder({ page })
    .setLegacyMode()
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  if (results.violations.length === 0) return;
  const details = results.violations.map((violation) => {
    const targets = violation.nodes
      .flatMap((node) => node.target)
      .slice(0, 4)
      .join(", ");
    return `${violation.id}: ${violation.help} [${targets}]`;
  });
  throw new Error(`${label} 未通过 WCAG 2.2 AA 自动扫描：\n${details.join("\n")}`);
}
