# Native conversation_updated jobs rebuild their payload at execution time.
# Skip only the discarded listener projection; retain native recipients and job.
require 'digest'

module NevidimyConversationBroadcastFastPath
  CALLBACK_SHA256 = '8b3d3aa46bc905b20a889154e3d26145b8b0884f6756d8a7bbbdcd82eb8c2628'.freeze
  BROADCAST_SHA256 = 'e8a3f49f55466b26ca9d24b0e8e4bebb26a4b6d7209430a9039f7f9490c58a60'.freeze
  PERFORM_SHA256 = '6dcfd66ddb5e59610cf445b8a0c2daad9a37dc2c575388f01caaca2cd4eb1fde'.freeze
  REFRESH_SHA256 = '0620b4475a26b19b5aec422339c33c5799e579c82c777d3a0771a75c5143f030'.freeze

  def self.native_method?(type, name, arity, lines, digest)
    method = type.instance_method(name)
    return false unless method.owner == type && method.arity == arity && method.source_location

    path, line = method.source_location
    Digest::SHA256.hexdigest(File.readlines(path)[line - 1, lines].join) == digest
  end

  def self.native_contract?(listener, job)
    native_method?(listener, :conversation_updated, 1, 6, CALLBACK_SHA256) &&
      native_method?(listener, :broadcast, 4, 10, BROADCAST_SHA256) &&
      native_method?(job, :perform, 3, 6, PERFORM_SHA256) &&
      native_method?(job, :prepare_broadcast_data, 2, 19, REFRESH_SHA256) &&
      job.const_get(:CONVERSATION_UPDATE_EVENTS, false).include?(Events::Types::CONVERSATION_UPDATED)
  rescue StandardError
    # Unknown source, owners or refresh behavior retain the full native path.
    false
  end

  module Listener
    def conversation_updated(event)
      conversation, account = extract_conversation_and_account(event)
      tokens = user_tokens(account, conversation.inbox.members) + contact_inbox_tokens(conversation.contact_inbox)

      broadcast(account, tokens, Events::Types::CONVERSATION_UPDATED, {id: conversation.display_id})
    end
  end
end

# Load before nevidimy_turn_timing.rb so its observation wrapper remains outermost.
Rails.application.reloader.to_prepare do
  next unless defined?(ActionCableListener) && defined?(ActionCableBroadcastJob)
  next if ActionCableListener < NevidimyConversationBroadcastFastPath::Listener
  next unless NevidimyConversationBroadcastFastPath.native_contract?(ActionCableListener, ActionCableBroadcastJob)

  ActionCableListener.prepend(NevidimyConversationBroadcastFastPath::Listener)
end
