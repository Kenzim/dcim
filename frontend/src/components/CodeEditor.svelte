<script>
  import { onMount, onDestroy } from 'svelte';
  
  export let value = '';
  export let language = 'bash';
  export let theme = 'dark';
  
  let container;
  let view = null;
  let modules = null;

  async function initEditor() {
    if (!container || typeof window === 'undefined') return;
    
    try {
      // Dynamically import CodeMirror modules
      const codemirror = await import('codemirror');
      const { EditorView, basicSetup } = codemirror;
      const { EditorState } = await import('@codemirror/state');
      
      // Note: Syntax highlighting temporarily disabled due to compatibility issues
      // const { StreamLanguage } = await import('@codemirror/language');
      // const { shell } = await import('@codemirror/legacy-modes/mode/shell');
      
      modules = { EditorView, EditorState, basicSetup };
      
      // Destroy existing editor if any
      if (view) {
        view.destroy();
      }
      
      // Temporarily disable syntax highlighting due to compatibility issues
      // between StreamLanguage (legacy-modes) and oneDark theme highlighting system
      // The editor will work perfectly fine, just without syntax colors for now
      let langExtension = null;
      // TODO: Re-enable when we find a compatible highlighting solution
      // if (language === 'bash' || language === 'shell') {
      //   try {
      //     const shellLang = StreamLanguage.define(shell);
      //     langExtension = shellLang;
      //   } catch (langError) {
      //     console.warn('Failed to load shell language highlighting:', langError);
      //     langExtension = null;
      //   }
      // }
      
      // Use a basic dark theme instead of oneDark to avoid highlighting conflicts
      // We'll create a simple dark theme manually
      let themeExtension = null;
      if (theme === 'dark') {
        // Create a simple dark theme without highlighting conflicts
        themeExtension = EditorView.theme({
          '&': {
            backgroundColor: '#1e293b',
            color: '#e2e8f0'
          },
          '.cm-content': {
            caretColor: '#e2e8f0'
          },
          '.cm-scroller': {
            fontFamily: "'Courier New', 'Monaco', 'Menlo', monospace"
          },
          '&.cm-focused .cm-cursor': {
            borderLeftColor: '#e2e8f0'
          },
          '&.cm-focused .cm-selectionBackground, .cm-selectionBackground': {
            backgroundColor: '#334155'
          },
          '.cm-gutters': {
            backgroundColor: '#0f172a',
            color: '#64748b',
            border: 'none'
          },
          '.cm-lineNumbers .cm-gutterElement': {
            minWidth: '3ch',
            padding: '0 8px 0 8px'
          }
        }, { dark: true });
      }
      
      // Build extensions array
      const extensions = [basicSetup];
      
      // Skip language extension for now (syntax highlighting disabled)
      // if (langExtension) {
      //   extensions.push(langExtension);
      // }
      
      // Add simple dark theme
      if (themeExtension) {
        extensions.push(themeExtension);
      }
      
      extensions.push(
        EditorView.updateListener.of(update => {
          if (update.docChanged) {
            value = update.state.doc.toString();
          }
        })
      );
      
      const state = EditorState.create({
        doc: value,
        extensions
      });

      view = new EditorView({
        state,
        parent: container
      });
    } catch (error) {
      console.error('Failed to load CodeMirror:', error);
      // Fallback to textarea if CodeMirror fails to load
      const textarea = document.createElement('textarea');
      textarea.value = value;
      textarea.style.cssText = 'width: 100%; height: 400px; font-family: monospace; padding: 12px; border-color: var(--border-color); border-radius: 8px;';
      textarea.addEventListener('input', (e) => {
        value = e.target.value;
      });
      container.innerHTML = '';
      container.appendChild(textarea);
    }
  }

  function updateContent(newValue) {
    if (!view || !modules) return;
    
    const { EditorState } = modules;
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: newValue }
    });
  }

  $: if (value && view && modules) {
    const currentContent = view.state.doc.toString();
    if (currentContent !== value) {
      updateContent(value);
    }
  }

  onMount(() => {
    initEditor();
  });

  onDestroy(() => {
    if (view) {
      view.destroy();
      view = null;
    }
  });
</script>

<div bind:this={container} class="code-editor"></div>

<style>
  .code-editor {
    min-height: 400px;
    border-color: var(--border-color);
    border-radius: 8px;
    overflow: hidden;
  }

  .code-editor :global(.cm-editor) {
    height: 400px;
  }

  .code-editor :global(.cm-scroller) {
    overflow: auto;
    font-family: 'Courier New', 'Monaco', 'Menlo', monospace;
    font-size: 14px;
  }

  .code-editor :global(textarea) {
    width: 100%;
    height: 400px;
    font-family: 'Courier New', 'Monaco', 'Menlo', monospace;
    padding: 12px;
    border-color: var(--border-color);
    border-radius: 8px;
    resize: vertical;
  }
</style>
