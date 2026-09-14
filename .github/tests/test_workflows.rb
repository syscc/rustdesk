# frozen_string_literal: true

require 'minitest/autorun'
require 'yaml'

class WorkflowContractTest < Minitest::Test
  ROOT = File.expand_path('../..', __dir__)
  BUILD_WORKFLOW = File.join(ROOT, '.github', 'workflows', 'flutter-build.yml')
  TAG_WORKFLOW = File.join(ROOT, '.github', 'workflows', 'flutter-tag.yml')
  SOURCE_SHA_REF = '${{ needs.inspect-source.outputs.source-sha }}'
  RELEASE_ACTION = 'softprops/action-gh-release@efb35369e0ad2afab669f228072c1b0d510eae64'

  def setup
    @build = YAML.load_file(BUILD_WORKFLOW)
    @tag = YAML.load_file(TAG_WORKFLOW)
    @build_jobs = @build.fetch('jobs')
    @tag_jobs = @tag.fetch('jobs')
  end

  def test_inspect_source_publishes_the_complete_source_contract
    inspect = @build_jobs.fetch('inspect-source')
    expected_outputs = {
      'source-sha' => '${{ steps.inspect.outputs.source-sha }}',
      'version' => '${{ steps.inspect.outputs.version }}',
      'vcpkg-commit' => '${{ steps.inspect.outputs.vcpkg-commit }}',
      'supports-drm' => '${{ steps.inspect.outputs.supports-drm }}',
      'supports-msi-template' => '${{ steps.inspect.outputs.supports-msi-template }}',
      'needs-pam' => '${{ steps.inspect.outputs.needs-pam }}',
      'needs-nasm2' => '${{ steps.inspect.outputs.needs-nasm2 }}'
    }

    assert_equal expected_outputs, inspect.fetch('outputs')
    assert_equal 'write', inspect.dig('permissions', 'contents'),
                 'preflight must be able to create/update the target release before parallel publishers run'
    inspect_step = steps(inspect).find { |step| step['id'] == 'inspect' }
    refute_nil inspect_step
    assert_equal 'syscc/rustdesk/.github/actions/inspect-build-source@main', inspect_step['uses']
    prepare_release = release_steps(inspect).find { |step| step['name'] == 'Prepare release' }
    refute_nil prepare_release
    assert_equal '${{ github.sha }}', prepare_release.dig('with', 'target_commitish'),
                 'the release tag must target a commit in the publishing repository'
  end

  def test_source_jobs_and_release_consumers_directly_need_inspect_source
    source_jobs = @build_jobs.select do |name, job|
      next false if name == 'inspect-source'

      has_source_checkout = checkout_steps(job).any?
      is_nested_bridge = job['uses'] == './.github/workflows/bridge.yml'
      uses_source_outputs = job.fetch('env', {}).values.any? do |value|
        value.to_s.include?('needs.inspect-source.outputs.')
      end
      has_source_checkout || is_nested_bridge || uses_source_outputs
    end

    release_consumers = @build_jobs.select do |name, job|
      name != 'inspect-source' && release_steps(job).any?
    end

    refute_empty source_jobs
    refute_empty release_consumers
    (source_jobs.keys + release_consumers.keys).uniq.each do |name|
      assert_includes needs_for(@build_jobs.fetch(name)), 'inspect-source',
                      "#{name} must directly depend on inspect-source"
    end
  end

  def test_every_selected_source_checkout_uses_inspected_source_sha
    source_jobs = @build_jobs.select do |name, job|
      name != 'inspect-source' && checkout_steps(job).any?
    end

    refute_empty source_jobs
    source_jobs.each do |name, job|
      checkout_steps(job).each do |checkout|
        assert_equal SOURCE_SHA_REF, checkout.dig('with', 'ref'),
                     "#{name} must checkout needs.inspect-source.outputs.source-sha"
      end
    end
  end

  def test_version_and_vcpkg_values_only_come_from_inspect_source_outputs
    global_env = @build.fetch('env')
    refute global_env.key?('VERSION'), 'VERSION must not be globally hardcoded'
    refute global_env.key?('VCPKG_COMMIT_ID'), 'VCPKG_COMMIT_ID must not be globally hardcoded'

    expected = {
      'VERSION' => '${{ needs.inspect-source.outputs.version }}',
      'VCPKG_COMMIT_ID' => '${{ needs.inspect-source.outputs.vcpkg-commit }}'
    }
    @build_jobs.each do |name, job|
      job.fetch('env', {}).each do |key, value|
        next unless expected.key?(key)

        assert_equal expected.fetch(key), value,
                     "#{name}.env.#{key} must use inspect-source output"
      end
    end
  end

  def test_linux_flutter_drm_and_sciter_have_pam_and_safe_asset_mounts
    %w[build-rustdesk-linux build-rustdesk-linux-drm].each do |name|
      job = @build_jobs.fetch(name)
      arch_steps = run_on_arch_steps(job)
      refute_empty arch_steps, "#{name} must have a run-on-arch build step"

      arch_steps.each do |step|
        args = step.dig('with', 'dockerRunArgs').to_s
        asset_mounts = args.lines.map(&:strip).select { |line| line.include?('SYSCC_BUILD_ASSETS') && line.include?('--volume') }
        assert_equal 1, asset_mounts.length,
                     "#{name} must have exactly one build-assets volume"
        assert_equal '--volume "${SYSCC_BUILD_ASSETS}:/syscc-build-assets:ro"', asset_mounts.first,
                     "#{name} must mount build assets read-only"
        assert_includes args, '--env "SYSCC_BUILD_ASSETS=/syscc-build-assets"',
                        "#{name} must expose the container asset path"

        commands = step.dig('with', 'run').to_s.lines.map(&:strip).reject do |line|
          line.empty? || line.start_with?('#')
        end
        assert_match(/\Atest -r "\$SYSCC_BUILD_ASSETS\/[^"\n]+"\z/, commands.first.to_s,
                     "#{name} must check the helper before the container build")
        assert_includes step.dig('with', 'install').to_s, 'libpam0g-dev',
                        "#{name} must install PAM development headers"
      end
    end

    sciter = @build_jobs.fetch('build-rustdesk-linux-sciter')
    sciter_arch_steps = run_on_arch_steps(sciter)
    refute_empty sciter_arch_steps
    sciter_arch_steps.each do |step|
      assert_includes step.dig('with', 'install').to_s, 'libpam0g-dev',
                      'build-rustdesk-linux-sciter must install PAM development headers'
    end
  end

  def test_drm_build_is_disabled_when_the_source_does_not_support_drm
    condition = @build_jobs.fetch('build-rustdesk-linux-drm').fetch('if').to_s
    assert_match(/needs\.inspect-source\.outputs\.supports-drm\s*==\s*'true'/, condition)
  end

  def test_msi_template_paths_are_capability_guarded
    windows = @build_jobs.fetch('build-for-windows-flutter')
    msi_build = steps(windows).find do |step|
      step['name'].to_s.downcase.include?('msi template') && step['run']
    end
    msi_upload = steps(windows).find do |step|
      step['name'].to_s.downcase.include?('msi template') && step['uses'].to_s.start_with?('actions/upload-artifact@')
    end

    refute_nil msi_build
    refute_nil msi_upload
    [msi_build, msi_upload].each do |step|
      assert_capability_guard(step, 'supports-msi-template')
    end

    unsigned = @build_jobs.fetch('publish_unsigned')
    msi_downloads = steps(unsigned).select do |step|
      step['uses'].to_s.start_with?('actions/download-artifact@') &&
        step.dig('with', 'name').to_s.include?('msi-template')
    end
    assert_equal 2, msi_downloads.length, 'both MSI template downloads must be present'
    msi_downloads.each { |step| assert_capability_guard(step, 'supports-msi-template') }
  end

  def test_unsigned_tar_includes_msi_template_only_when_supported
    combine = steps(@build_jobs.fetch('publish_unsigned')).find { |step| step['name'] == 'Combine unsigned app' }
    refute_nil combine
    script = combine.fetch('run')

    conditional_block = script[/if \[\[.*supports-msi-template.*?\nfi/m]
    refute_nil conditional_block, 'MSI template must be appended inside a capability condition'
    assert_includes conditional_block, 'files+=( msi-template )'
    refute_match(/^[ \t]*files=\([^)\n]*\bmsi-template\b/m, script)
    assert_match(/tar czf .*rustdesk-\$\{\{ env\.VERSION \}\}-unsigned\.tar\.gz/, script)
  end

  def test_every_release_action_is_pinned_and_requires_publish_release
    releases = @build_jobs.values.flat_map { |job| release_steps(job) }
    refute_empty releases

    releases.each do |step|
      assert_equal RELEASE_ACTION, step['uses'],
                   "#{step['name'] || 'unnamed release step'} must use the approved release pin"
      condition = step['if'].to_s.gsub('${{', '').gsub('}}', '').strip
      assert publish_release_is_mandatory?(condition),
             "#{step['name'] || 'unnamed release step'} must be blocked when inputs.publish-release is false"
    end
  end

  def test_release_assets_target_a_commit_in_the_publishing_repository
    @build_jobs.each do |name, job|
      release_steps(job).each do |step|
        assert_equal '${{ github.sha }}', step.dig('with', 'target_commitish'),
                     "#{name}.#{step['name'] || step['id']} must target the publishing repository"
      end
    end
  end

  def test_publish_release_false_cannot_enable_a_release_while_uploading_artifacts
    build_inputs = event_config(@build).fetch('workflow_call').fetch('inputs')
    assert_equal true, build_inputs.fetch('publish-release').fetch('default')

    tag_inputs = event_config(@tag).fetch('workflow_dispatch').fetch('inputs')
    assert_equal true, tag_inputs.fetch('upload-artifact').fetch('default')
    assert_equal true, tag_inputs.fetch('publish-release').fetch('default')

    forwarded_upload = @tag_jobs.fetch('run-flutter-tag-build').dig('with', 'upload-artifact').to_s
    forwarded_publish = @tag_jobs.fetch('run-flutter-tag-build').dig('with', 'publish-release').to_s
    assert_match(/github\.event_name\s*==\s*'push'/, forwarded_upload)
    assert_match(/inputs\.upload-artifact/, forwarded_upload)
    refute_match(/publish-release/, forwarded_upload,
                  'packaging/uploading must remain enabled for an unpublished test')
    assert_match(/inputs\.publish-release/, forwarded_publish)

    @build_jobs.values.flat_map { |job| release_steps(job) }.each do |step|
      condition = step['if'].to_s.gsub('${{', '').gsub('}}', '').strip
      assert publish_release_is_mandatory?(condition)
    end
  end

  def test_tag_entrypoint_honours_upload_tag_and_serializes_the_same_target
    tag_inputs = event_config(@tag).fetch('workflow_dispatch').fetch('inputs')
    assert_equal 'nightly', tag_inputs.fetch('upload-tag').fetch('default')

    assert_equal '${{ inputs.upload-tag }}', @build.fetch('env').fetch('TAG_NAME')

    tag_job = @tag_jobs.fetch('run-flutter-tag-build')
    assert_equal 'inherit', tag_job.fetch('secrets')
    upload_tag = tag_job.dig('with', 'upload-tag').to_s
    assert_match(/github\.event_name\s*==\s*'push'/, upload_tag)
    assert_match(/github\.ref_name/, upload_tag)
    assert_match(/inputs\.upload-tag/, upload_tag)

    concurrency = @tag.fetch('concurrency')
    assert_equal false, concurrency.fetch('cancel-in-progress')
    group = concurrency.fetch('group').to_s
    assert_match(/github\.ref_name/, group)
    assert_match(/inputs\.upload-tag/, group)
  end

  def test_generate_sbom_has_upload_and_one_bounded_retry_with_identical_release_inputs
    job = @build_jobs.fetch('generate-sbom')
    sbom_uploads = steps(job).select { |step| step['name'] == 'Upload SBOM artifact' }
    assert_equal 1, sbom_uploads.length
    sbom_upload = sbom_uploads.first
    assert_equal 'inputs.upload-artifact', sbom_upload.fetch('if')
    assert_equal 'actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a', sbom_upload.fetch('uses')
    assert_equal 'rustdesk-sbom', sbom_upload.dig('with', 'name')
    assert_equal 'rustdesk.sbom.json', sbom_upload.dig('with', 'path')
    assert_equal 'error', sbom_upload.dig('with', 'if-no-files-found')

    writers = steps(job).select do |step|
      step['uses'].to_s.start_with?('softprops/action-gh-release@') &&
        step.dig('with', 'files').to_s.strip == 'rustdesk.sbom.json'
    end
    assert_equal 2, writers.length, 'SBOM publication must have one initial writer and one retry only'

    first = writers.find { |step| step['id'] == 'publish-sbom-first' }
    retry_step = writers.find { |step| step['name'] == 'Retry SBOM publication' }
    refute_nil first
    refute_nil retry_step
    assert_equal true, first.fetch('continue-on-error')
    refute retry_step.key?('continue-on-error'), 'the bounded retry must fail the job on a second failure'
    assert_equal first.fetch('uses'), retry_step.fetch('uses')
    assert_equal without_key(first.fetch('with'), 'files'), without_key(retry_step.fetch('with'), 'files'),
                 'the retry must keep the same release target inputs'
    assert_equal first.dig('with', 'files').to_s.strip, retry_step.dig('with', 'files').to_s.strip,
                 'the retry must publish the same SBOM file'

    [first, retry_step].each do |writer|
      condition = writer.fetch('if').to_s
      assert publish_release_is_mandatory?(condition),
             "#{writer['name'] || writer['id']} must hard-gate inputs.publish-release"
      assert_match(/inputs\.publish-release/, condition)
    end
    assert_match(/env\.UPLOAD_ARTIFACT\s*==\s*'true'/, first.fetch('if'))
    assert_match(/inputs\.upload-artifact/, retry_step.fetch('if'))

    wait = steps(job).find { |step| step['name'] == 'Wait before retrying SBOM publication' }
    refute_nil wait
    assert_equal 'sleep 20', wait.fetch('run').to_s.strip
    assert_equal retry_step.fetch('if'), wait.fetch('if')
    assert_match(/steps\.publish-sbom-first\.outcome\s*==\s*'failure'/, wait.fetch('if'))

    first_index = steps(job).index(first)
    wait_index = steps(job).index(wait)
    retry_index = steps(job).index(retry_step)
    assert_equal first_index + 1, wait_index
    assert_equal wait_index + 1, retry_index
  end

  def test_windows_jobs_select_compatible_nasm_between_vcpkg_setup_and_install
    %w[build-for-windows-flutter build-for-windows-sciter].each do |name|
      job = @build_jobs.fetch(name)
      assert_includes needs_for(job), 'inspect-source'

      vcpkg_steps = steps(job).select { |step| step['uses'].to_s.start_with?('lukka/run-vcpkg@') }
      nasm_steps = steps(job).select { |step| step['name'] == 'Select compatible Windows NASM' }
      install_steps = steps(job).select { |step| step['name'] == 'Install vcpkg dependencies' }
      assert_equal 1, vcpkg_steps.length, "#{name} must have one vcpkg setup step"
      assert_equal 1, nasm_steps.length, "#{name} must define one optional NASM selector"
      assert_equal 1, install_steps.length, "#{name} must have one vcpkg install step"

      vcpkg_index = steps(job).index(vcpkg_steps.first)
      nasm_index = steps(job).index(nasm_steps.first)
      install_index = steps(job).index(install_steps.first)
      assert_equal vcpkg_index + 1, nasm_index,
                   "#{name} must select NASM immediately after run-vcpkg"
      assert_equal nasm_index + 1, install_index,
                   "#{name} must verify NASM before installing vcpkg dependencies"

      nasm = nasm_steps.first
      assert_match(/needs\.inspect-source\.outputs\.needs-nasm2\s*==\s*'true'/, nasm.fetch('if'))
      assert_equal 'syscc/rustdesk/.github/actions/setup-windows-nasm@main', nasm.fetch('uses')
      assert_equal '${{ env.VCPKG_ROOT }}', nasm.dig('with', 'vcpkg-root')
      assert_equal '${{ needs.inspect-source.outputs.vcpkg-commit }}', nasm.dig('with', 'expected-commit')

      install_script = install_steps.first.fetch('run')
      assert_match(/needs\.inspect-source\.outputs\.needs-nasm2/, install_script)
      assert_includes install_script, 'where.exe nasm'
      assert_includes install_script, "nasm -v | grep -F 'NASM version 2.16.03'"
    end
  end

  def test_nested_bridge_receives_inspected_sha_and_inherits_secrets
    bridge = @build_jobs.fetch('generate-bridge')
    assert_equal ['inspect-source'], needs_for(@build_jobs.fetch('generate-bridge'))
    assert_equal 'inherit', bridge.fetch('secrets')
    assert_equal SOURCE_SHA_REF, bridge.dig('with', 'source-ref')
  end

  private

  def event_config(workflow)
    workflow['on'] || workflow[true] || workflow.fetch('on')
  end

  def steps(job)
    job.fetch('steps', [])
  end

  def without_key(hash, key)
    copy = hash.dup
    copy.delete(key)
    copy
  end

  def shell_lines(script)
    script.to_s.lines.map(&:strip).reject do |line|
      line.empty? || line.start_with?('#')
    end
  end

  def checkout_steps(job)
    steps(job).select { |step| step['uses'].to_s.start_with?('actions/checkout@') }
  end

  def release_steps(job)
    steps(job).select { |step| step['uses'].to_s.start_with?('softprops/action-gh-release@') }
  end

  def run_on_arch_steps(job)
    steps(job).select { |step| step['uses'].to_s.start_with?('rustdesk-org/run-on-arch-action@') }
  end

  def needs_for(job)
    value = job['needs']
    value.nil? ? [] : (value.is_a?(Array) ? value : [value])
  end

  def assert_capability_guard(step, capability)
    condition = step['if'].to_s
    assert_match(/needs\.inspect-source\.outputs\[?\.?#{Regexp.escape(capability)}\]?\s*==\s*'true'/, condition,
                 "#{step['name'] || 'step'} must require #{capability}")
  end

  def publish_release_is_mandatory?(condition)
    expression = strip_outer_parentheses(condition)
    return true if expression == 'inputs.publish-release'

    alternatives = split_top_level(expression, '||')
    return alternatives.all? { |branch| publish_release_is_mandatory?(branch) } if alternatives.length > 1

    terms = split_top_level(expression, '&&')
    return false if terms.length == 1

    terms.any? { |term| publish_release_is_mandatory?(term) }
  end

  def split_top_level(expression, operator)
    parts = []
    depth = 0
    start = 0
    index = 0

    while index < expression.length
      character = expression[index]
      depth += 1 if character == '('
      depth -= 1 if character == ')'
      if depth.zero? && expression[index, operator.length] == operator
        parts << expression[start...index]
        start = index + operator.length
        index += operator.length
      else
        index += 1
      end
    end

    parts << expression[start..-1]
    parts
  end

  def strip_outer_parentheses(expression)
    result = expression.to_s.strip
    loop do
      break unless result.start_with?('(') && result.end_with?(')')

      depth = 0
      closing_index = nil
      result.each_char.with_index do |character, index|
        depth += 1 if character == '('
        depth -= 1 if character == ')'
        if depth.zero?
          closing_index = index
          break
        end
      end
      break unless closing_index == result.length - 1

      result = result[1...-1].strip
    end
    result
  end
end
