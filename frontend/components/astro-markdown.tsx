import ReactMarkdown, { type Components } from "react-markdown";
import { cn } from "@/lib/utils";

// Renders Astro AI's assistant replies as Markdown inside the existing chat
// bubble. react-markdown parses to React elements (never raw HTML), so this
// stays safe against a malicious/malformed model response without needing
// dangerouslySetInnerHTML or a separate sanitizer -- there is no HTML string
// for either of us to get wrong.
//
// Every element below is restyled to inherit the bubble's own text-sm/leading
// rather than pulling in something like @tailwindcss/typography, which would
// bring its own font sizes/colors/margins not present anywhere else in this
// design system.
const components: Components = {
  p: ({ children }) => <p className="mb-2 leading-normal last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  ul: ({ children }) => <ul className="mb-2 list-disc space-y-0.5 pl-5 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal space-y-0.5 pl-5 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="leading-normal">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="underline underline-offset-2 hover:text-primary"
    >
      {children}
    </a>
  ),
  code: ({ className, children }) => {
    // react-markdown gives fenced code blocks a `language-xxx` className on
    // the inner <code>; plain inline code has none. Same component, two looks.
    const isBlock = typeof className === "string" && className.startsWith("language-");
    return (
      <code
        className={cn(
          "break-words rounded bg-foreground/10 px-1 py-0.5 font-mono text-[0.85em]",
          isBlock && "block overflow-x-auto whitespace-pre px-2 py-1.5"
        )}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => <pre className="mb-2 max-w-full overflow-x-auto last:mb-0">{children}</pre>,
  h1: ({ children }) => <p className="mb-2 font-semibold last:mb-0">{children}</p>,
  h2: ({ children }) => <p className="mb-2 font-semibold last:mb-0">{children}</p>,
  h3: ({ children }) => <p className="mb-2 font-semibold last:mb-0">{children}</p>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-border pl-3 text-foreground/80 last:mb-0">{children}</blockquote>
  ),
};

export function AstroMarkdown({ content }: { content: string }) {
  return (
    <div className="break-words [&>*:first-child]:mt-0">
      <ReactMarkdown components={components}>{content}</ReactMarkdown>
    </div>
  );
}
