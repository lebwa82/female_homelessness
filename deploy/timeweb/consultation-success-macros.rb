# Run explicitly inside the current Chatwoot Rails runtime after backend deployment.
# This file never deploys the application or modifies conversations.
account = Account.find(Integer(ENV.fetch('CHATWOOT_ACCOUNT_ID', '1')))
admin = account.account_users.where(role: :administrator).order(:id).first&.user
raise 'account administrator required' unless admin

definitions = [
  ['Консультация психолога прошла успешно', '[women-help:consultation-success:psychologist_3_sessions]'],
  ['Консультация юриста прошла успешно', '[women-help:consultation-success:legal_consultation]']
]
Account.transaction do
  definitions.each do |name, command|
    actions = [{'action_name' => 'add_private_note', 'action_params' => [command]}]
    macro = account.macros.find_or_initialize_by(name: name, visibility: :global)
    if macro.new_record?
      macro.assign_attributes(actions: actions, created_by: admin, updated_by: admin)
      macro.save!
    elsif macro.actions.as_json != actions
      raise 'existing success macro was edited; preserve it and review manually'
    end
  end
end
puts({success_macros_ready: definitions.map(&:first)}.to_json)
